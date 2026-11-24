import asyncio
from datetime import date
from decimal import Decimal
from io import BytesIO
import json
import sys
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from zipfile import ZipFile

import httpx
import numpy as np
import pytest

from backend import dart_etl as etl, dart_rag as rag
from backend.dart_chunks import chunk_document


CORP = "00126380"
RECEIPT = "20260115000001"
DAY = date(2026, 1, 15)
VECTOR = [1] + [0] * 1023


def run(coro):
    if sys.platform == "win32":  # libpq async connections require a selector loop.
        with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
            return runner.run(coro)
    return asyncio.run(coro)


def bond(**changes):
    return {"corp_code": CORP, "bd_tm": None, "bdis_mthn": "사모",
            "bddd": "2026년 01월 15일", "cvisstk_cnt": "1,012", "cv_prc": "9,000.20",
            "cvrqpd_bgd": "20270115", "cvrqpd_edd": "-", "rcept_no": RECEIPT, **changes}


def test_null_issue_correction_and_out_of_order_replay(db):
    original = etl.parse_event(CORP, "CB", bond())
    corrected = etl.parse_event(CORP, "CB", bond(bd_tm="-", cvisstk_cnt="803",
                                cv_prc="10,000.15", rcept_no="20260116000002"))
    assert type(corrected[4]) is int and type(corrected[5]) is Decimal
    db.execute(etl.UPSERT_EVENT, original)
    db.execute(etl.UPSERT_EVENT, corrected)
    query = "SELECT bond_type_issue_no, convertible_shares, conversion_price, rcept_no FROM overhang_events"
    expected = [(None, 803, Decimal("10000.15"), "20260116000002")]
    assert db.execute(query).fetchall() == expected
    db.execute(etl.UPSERT_EVENT, original)
    assert db.execute(query).fetchall() == expected


@pytest.mark.parametrize("kind,quantity,period", [
    ("BW", "nstk_isstk_cnt", "expd"), ("EB", "extg_stkcnt", "exrqpd"),
])
def test_other_bond_fields(kind, quantity, period):
    row = bond(**{quantity: "9,007,199,254,740,993", "bd_tm": "3", "ex_prc": "1,234.01",
                  period + "_bgd": "2026.1.15", period + "_edd": "2027-01-16"})
    assert etl.parse_event(CORP, kind, row) == (
        CORP, "3", DAY, kind, 9007199254740993, Decimal("1234.01"), DAY, date(2027, 1, 16), RECEIPT)


@pytest.mark.parametrize("change", [
    {"cvisstk_cnt": "1.7"}, {"cvisstk_cnt": "NaN"}, {"cv_prc": "1.234"},
    {"bddd": "-"}, {"corp_code": "00000001"}, {"rcept_no": "1234"},
])
def test_malformed_event_is_rejected(change):
    with pytest.raises(ValueError):
        etl.parse_event(CORP, "CB", bond(**change))


def test_markdown_table_split_keeps_header_separator_and_all_rows():
    rows = [f"<TR><TE>사채 {i}</TE><TU>{i}</TU></TR>" for i in range(30)]
    markup = "<P>개요</P><TABLE><TR><TH>항목</TH><TH>금액</TH></TR>" + "".join(rows) + "</TABLE><P>끝</P>"
    before, *tables, after = chunk_document(markup, max_chars=85)
    assert (before, after) == ("개요", "끝")
    assert len(tables) > 1
    assert all(t.startswith("| 항목 | 금액 |\n| --- | --- |\n") and len(t) <= 85 for t in tables)
    assert [row for t in tables for row in t.splitlines()[2:]] == [
        f"| 사채 {i} | {i} |" for i in range(30)]


def test_spanning_headers_nested_table_and_long_escaped_cell():
    markup = """<table><thead><tr><th rowspan="2">항목</th><th colspan="2">금액</th></tr>
        <tr><th>현재</th><th>최초</th></tr></thead><tr><td>CB</td><td>10</td><td>9</td></tr>
        <tr><td><table><tr><td>특약</td></tr><tr><td>A|B</td></tr></table></td></tr></table>"""
    outer, inner = chunk_document(markup)
    assert outer.startswith("| 항목 | 금액 / 현재 | 금액 / 최초 |\n")
    assert "특약" not in outer and inner.endswith(r"| A\|B |")
    chunks = chunk_document("<table><tr><td>조항</td></tr><tr><td>" + ("가|나" * 60)
                            + "</td></tr></table>", 43)
    assert all(len(c) <= 43 and c.startswith("| 조항 |\n| --- |\n") for c in chunks)
    assert "".join(c.splitlines()[2][2:-2] for c in chunks) == r"가\|나" * 60


def test_oversized_single_cell_dart_table_keeps_all_text():
    text = "공시|본문\\내용" * 500
    chunks = chunk_document(f"<TABLE><TR><TE>{text}</TE></TR></TABLE>")
    assert len(chunks) > 1
    assert all(len(chunk) <= 1800 and chunk.endswith("\n| --- |") for chunk in chunks)
    assert "".join(chunk.splitlines()[0][2:-2] for chunk in chunks) == text.replace(
        "\\", "\\\\").replace("|", "\\|")


def test_oversized_table_preserves_values_under_their_column_headings():
    markup = """<table><thead><tr><th>만기 상환 대상자</th><th>조기 상환 요청 금액</th></tr></thead>
        <tr><td>원금 Z1</td><td>금액 70</td></tr>
        <tr><td>원금 X2</td><td>금액 80</td></tr></table>"""
    chunks = chunk_document(markup, max_chars=35)
    assert all(len(chunk) <= 35 for chunk in chunks)
    columns = {}
    for chunk in chunks:
        heading, separator, *rows = chunk.splitlines()
        assert separator == "| --- |"
        columns.setdefault(heading, []).extend(rows)
    assert columns == {
        "| 만기 상환 대상자 |": ["| 원금 Z1 |", "| 원금 X2 |"],
        "| 조기 상환 요청 금액 |": ["| 금액 70 |", "| 금액 80 |"],
    }


def test_kiwi_runs_in_to_thread_and_builds_searchable_tsvector(db, monkeypatch):
    main_thread = threading.get_ident()
    kiwi = MagicMock()

    def tokenize(text):
        assert threading.get_ident() != main_thread
        return [SimpleNamespace(form=form, tag=tag) for form, tag in
                [("전환", "NNG"), ("가액", "NNG"), ("을", "JKO"), ("CB", "SL")]]

    kiwi.tokenize.side_effect = tokenize
    monkeypatch.setattr(rag, "_kiwi", lambda: kiwi)
    offload = AsyncMock(wraps=asyncio.to_thread)
    monkeypatch.setattr(asyncio, "to_thread", offload)
    tokens = run(rag.keywords("전환가액을 CB"))
    offload.assert_awaited_once()
    assert tokens == ["전환", "가액", "CB"]
    db.execute(etl.INSERT_CHUNK, (CORP, RECEIPT, 0, "발행", DAY, "전환가액을 CB",
                                  json.dumps(VECTOR), " ".join(tokens)))
    rank, = db.execute("SELECT ts_rank(search_tsv, plainto_tsquery('simple', '전환 CB')) "
                       "FROM dart_chunks WHERE search_tsv @@ plainto_tsquery('simple', '가액')").fetchone()
    assert rank > 0


def test_exact_hybrid_candidates_are_scoped_before_limit_and_reranked(db, monkeypatch):
    # More competing exact matches from another issuer than the candidate budget.
    entries = [("00000002", i, VECTOR, "전환") for i in range(40)]
    entries.extend([(CORP, 100, VECTOR, "전환"), (CORP, 101, VECTOR, "기타"),
                    (CORP, 102, [0, 1] + [0] * 1022, "전환")])
    with db.cursor() as cursor:
        cursor.executemany(etl.INSERT_CHUNK, [
            (corp, f"{20260115000000 + i}", 0, "공시", DAY, str(i), json.dumps(vector), terms)
            for corp, i, vector, terms in entries])
    monkeypatch.setattr(rag, "keywords", AsyncMock(return_value=["전환"]))
    monkeypatch.setattr(rag, "embed", AsyncMock(return_value=[VECTOR]))
    rerank = AsyncMock(return_value=[0, 1])
    monkeypatch.setattr(rag, "rerank", rerank)
    rows = run(rag.search(CORP, "전환", candidates=2, limit=2))
    rerank.assert_awaited_once_with("전환", ["100", "101"])
    assert [row["chunk_text"] for row in rows] == ["101", "100"]
    assert rows[1]["hybrid_score"] > rows[0]["hybrid_score"]
    # Show both access paths are available even on a tiny fixture where the
    # cost-based planner would otherwise choose a sequential scan.
    with db.transaction():
        db.execute("SET LOCAL enable_seqscan = off")
        plan = db.execute("EXPLAIN " + rag.HYBRID_SQL, {"corp": CORP, "terms": "전환",
                          "vector": json.dumps(VECTOR), "candidates": 2,
                          "since": None, "until": None}).fetchall()
        assert "idx_dart_chunks_corp" in str(plan) and "CTE Scan" in str(plan)
        gin = db.execute("EXPLAIN SELECT * FROM dart_chunks WHERE search_tsv @@ "
                         "plainto_tsquery('simple','전환')").fetchall()
        assert "idx_dart_chunks_search" in str(gin)


def test_fp16_models_and_serial_inference_even_when_caller_is_cancelled(monkeypatch):
    encoder, ranker = MagicMock(), MagicMock()
    constructors = SimpleNamespace(SentenceTransformer=MagicMock(return_value=encoder),
                                   CrossEncoder=MagicMock(return_value=ranker))
    torch = SimpleNamespace(float16=object(), cuda=SimpleNamespace(is_available=lambda: True))
    monkeypatch.setitem(sys.modules, "torch", torch)
    monkeypatch.setitem(sys.modules, "sentence_transformers", constructors)
    rag._models.cache_clear()
    running = threading.Event()
    finish = threading.Event()
    sem = asyncio.Semaphore(1)
    assert rag.gpu_semaphore._value == 1
    monkeypatch.setattr(rag, "gpu_semaphore", sem)

    def encode(texts, **kwargs):
        assert sem.locked()
        running.set()
        assert finish.wait(5)
        return np.array([VECTOR])

    def predict(pairs, *, batch_size, show_progress_bar):
        assert sem.locked() and finish.is_set()
        assert batch_size == 8 and len(pairs) <= batch_size
        return np.ones(len(pairs))

    encoder.encode.side_effect = encode
    ranker.predict.side_effect = predict

    async def scenario():
        encoding = asyncio.create_task(rag.embed(["공시"]))
        assert await asyncio.to_thread(running.wait, 5)
        encoding.cancel()
        with pytest.raises(asyncio.CancelledError):
            await encoding
        ranking = asyncio.create_task(rag.rerank("가격", ["청크"] * 19))
        await asyncio.sleep(0)
        assert sem.locked() and not ranker.predict.called
        finish.set()
        assert len(await ranking) == 19
        assert not sem.locked()

    try:
        run(scenario())
        options = {"device": "cuda", "model_kwargs": {"torch_dtype": torch.float16}}
        constructors.SentenceTransformer.assert_called_once_with("BAAI/bge-m3", **options)
        constructors.CrossEncoder.assert_called_once_with("BAAI/bge-reranker-v2-m3", max_length=1024, **options)
        assert [len(call.args[0]) for call in ranker.predict.call_args_list] == [8, 8, 3]
        rag._models.cache_clear()
        torch.cuda.is_available = lambda: False
        with pytest.raises(RuntimeError, match="CUDA"):
            run(rag.embed(["공시"]))
    finally:
        finish.set()
        rag._models.cache_clear()


def test_stock_ingest_lock_pagination_and_retry(db, monkeypatch):
    db.execute("INSERT INTO stock_master(stock_code,corp_code,isin_cd,corp_name,listed_shares) "
               "VALUES ('005930', %s, 'KR7005930003', '보통주', 10), "
               "('005935', %s, 'KR7005931001', '우선주', 1)", (CORP, CORP))
    db.execute("INSERT INTO stock_master(stock_code,asset_type,isin_cd,corp_name,listed_shares) "
               "VALUES ('069500', 'ETF', 'KR7069500007', 'ETF', 20)")
    archive = BytesIO()
    with ZipFile(archive, "w") as package:
        package.writestr("원문.xml", "<TABLE><TR><TD>항목</TD></TR><TR><TD>전환</TD></TR></TABLE>")
    calls = []

    def handle(request):
        params = request.url.params
        endpoint = request.url.path.split("/")[-1]
        calls.append(endpoint)
        assert params["crtfc_key"] == "test"
        if endpoint == "document.xml":
            return httpx.Response(200, content=archive.getvalue())
        assert params["corp_code"] == CORP
        if endpoint == "list.json":
            i = int(params["page_no"])
            return httpx.Response(200, json={"status": "000", "total_page": 2, "list": [{
                "corp_code": CORP, "rcept_no": f"{20260115000000 + i}", "report_nm": "발행결정",
                "rcept_dt": "20260115"}]})
        if endpoint == "cvbdIsDecsn.json":
            return httpx.Response(200, json={"status": "000", "list": [bond()]})
        return httpx.Response(200, json={"status": "013"})

    lock = AsyncMock()
    redis = MagicMock()
    redis.lock.return_value = lock
    lock.acquire.side_effect = [False, True, True]
    monkeypatch.setattr(rag, "keywords", AsyncMock(return_value=["전환"]))
    monkeypatch.setattr(rag, "embed", AsyncMock(return_value=[VECTOR]))

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
            service = etl.DartETL(redis, http, "test")
            assert not await service.ingest_stock("069500", DAY, DAY)
            assert not await service.ingest_stock("unknown", DAY, DAY)
            assert not await service.ingest_stock("005935", DAY, DAY)
            assert calls == []
            assert await service.ingest_stock("005930", DAY, DAY)
            assert await service.ingest_stock("005935", DAY, DAY)

    run(scenario())
    assert calls.count("document.xml") == 2
    assert calls.count("list.json") == 4
    assert db.execute("SELECT count(*) FROM dart_chunks").fetchone() == (2,)
    assert db.execute("SELECT count(*) FROM overhang_events").fetchone() == (1,)
    assert all(call.args == ("etl_lock:" + CORP,) and call.kwargs["timeout"] == 600
               for call in redis.lock.call_args_list)
    assert all(call.kwargs == {"blocking": False} for call in lock.acquire.await_args_list)
    assert lock.release.await_count == 2


def test_failed_embedding_rolls_back_entire_receipt_and_can_retry(db, monkeypatch):
    table = "<table><tr><td>항목</td></tr><tr><td>금액</td></tr></table>"
    archive = BytesIO()
    with ZipFile(archive, "w") as package:
        package.writestr("filing.xml", table * 9)
    service = etl.DartETL(None, None, "test")
    monkeypatch.setattr(service, "_request", AsyncMock(return_value=archive.getvalue()))
    monkeypatch.setattr(rag, "keywords", AsyncMock(return_value=["금액"]))
    monkeypatch.setattr(rag, "embed", AsyncMock(side_effect=[
        [VECTOR] * 8, RuntimeError("inference interrupted"), [VECTOR] * 8, [VECTOR]]))
    report = {"rcept_no": RECEIPT, "rcept_dt": "20260115", "report_nm": "발행결정"}
    with pytest.raises(RuntimeError, match="inference interrupted"):
        run(service._index_report(CORP, report))
    assert db.execute("SELECT count(*) FROM dart_chunks").fetchone() == (0,)
    run(service._index_report(CORP, report))
    assert db.execute("SELECT count(*) FROM dart_chunks").fetchone() == (9,)
