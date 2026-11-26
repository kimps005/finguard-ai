"""Issuer-scoped exact retrieval, Korean tokenization and local GPU inference."""

import asyncio
from functools import lru_cache
import json

from psycopg.rows import dict_row

from backend import db


gpu_semaphore = asyncio.Semaphore(1)
BATCH_SIZE = 8


@lru_cache(maxsize=1)
def _kiwi():
    from kiwipiepy import Kiwi
    return Kiwi(num_workers=1)


def _tokens(text):
    return [token.form for token in _kiwi().tokenize(text)
            if token.tag.startswith("NN") or token.tag in {"SL", "SN", "SH", "XR"}]


async def keywords(text: str) -> list[str]:
    return await asyncio.to_thread(_tokens, text)


@lru_cache(maxsize=1)
def _models():
    import torch
    from sentence_transformers import CrossEncoder, SentenceTransformer

    if not torch.cuda.is_available():
        raise RuntimeError("DART RAG inference requires a CUDA GPU")
    options = {"device": "cuda", "model_kwargs": {"torch_dtype": torch.float16}}
    encoder = SentenceTransformer("BAAI/bge-m3", **options)
    reranker = CrossEncoder("BAAI/bge-reranker-v2-m3", max_length=1024, **options)
    return encoder, reranker


async def _gpu_call(fn, *args):
    # Cancellation cannot stop a running thread. Return the permit only when it exits.
    await gpu_semaphore.acquire()
    worker = asyncio.create_task(asyncio.to_thread(fn, *args))

    def finished(task):
        gpu_semaphore.release()
        if not task.cancelled():
            task.exception()  # Observe errors even if the requesting ETL was cancelled.

    worker.add_done_callback(finished)
    return await asyncio.shield(worker)


def _warm_models():
    encoder, reranker = _models()
    encoder.encode(["준비"], show_progress_bar=False)
    reranker.predict([("준비", "준비")], show_progress_bar=False)


async def warm_up():
    """Load Kiwi, bge-m3 and the reranker before the first request needs them.

    Cold, measured 2026-11-26: Kiwi 1.8s + model load 16.3s + first encode 1.0s. Left to the first price
    card, the disclosure search hit its 30s limit and the card was built without filings.
    """
    await asyncio.to_thread(lambda: _kiwi().split_into_sents("준비. 완료."))
    await _gpu_call(_warm_models)


def _encode(texts):
    encoder, _ = _models()
    return encoder.encode(texts, batch_size=BATCH_SIZE, normalize_embeddings=True,
                          show_progress_bar=False).tolist()


async def embed(texts: list[str]) -> list[list[float]]:
    return await _gpu_call(_encode, texts) if texts else []


def _rerank(query, texts):
    _, reranker = _models()
    scores = []
    for start in range(0, len(texts), BATCH_SIZE):
        pairs = [(query, text) for text in texts[start:start + BATCH_SIZE]]
        scores.extend(reranker.predict(pairs, batch_size=BATCH_SIZE, show_progress_bar=False).tolist())
    return scores


async def rerank(query: str, texts: list[str]) -> list[float]:
    return await _gpu_call(_rerank, query, texts) if texts else []


# Materialization fixes the exact scan's input set. The independent lexical
# branch can use GIN without excluding vector-only candidates from the result.
HYBRID_SQL = """
WITH issuer AS MATERIALIZED (
    SELECT rcept_no, chunk_no, report_nm, rcept_dt, chunk_text, embedding
    FROM dart_chunks WHERE corp_code = %(corp)s
      AND rcept_dt >= COALESCE(%(since)s::date, '-infinity'::date)
      AND rcept_dt <= COALESCE(%(until)s::date, 'infinity'::date)
), terms AS (
    SELECT websearch_to_tsquery('simple', %(terms)s) AS query
), lexical AS (
    SELECT rcept_no, chunk_no, ts_rank(search_tsv, query, 32) AS rank
    FROM dart_chunks, terms
    WHERE corp_code = %(corp)s AND search_tsv @@ query
)
SELECT rcept_no, chunk_no, report_nm, rcept_dt, chunk_text,
       1 - (embedding <=> %(vector)s::vector) + COALESCE(rank, 0) AS hybrid_score
FROM issuer LEFT JOIN lexical USING (rcept_no, chunk_no)
ORDER BY hybrid_score DESC, rcept_no DESC, chunk_no
LIMIT %(candidates)s
"""


async def search(corp_code: str, query: str, *, limit: int = 5, candidates: int = 32,
                 since=None, until=None) -> list[dict]:
    if not query.strip() or not 1 <= limit <= candidates:
        raise ValueError("search requires a query and 1 <= limit <= candidates")
    tokens = await keywords(query)
    vector, = await embed([query])
    params = {"corp": corp_code, "terms": " OR ".join(dict.fromkeys(tokens)),
              "vector": json.dumps(vector, allow_nan=False), "candidates": candidates,
              "since": since, "until": until}
    async with db.connection(row_factory=dict_row) as conn:
        # Prefer the issuer B-tree even before a newly ingested volume has statistics.
        await conn.execute("SET LOCAL enable_seqscan = off")
        cursor = await conn.execute(HYBRID_SQL, params)
        rows = await cursor.fetchall()
    scores = await rerank(query, [row["chunk_text"] for row in rows])
    for row, score in zip(rows, scores, strict=True):
        row["rerank_score"] = score
    return sorted(rows, key=lambda row: row["rerank_score"], reverse=True)[:limit]
