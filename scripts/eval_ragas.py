"""Evaluate issuer retrieval and the answer model's streamed portfolio report.

Each case runs the production chat graph: router, market data, scorecards and simulation, the real
price-card builder (fed a fixed market snapshot) and the answer model with its verifier. The analyst
is blocked in both modes so every card is the local card with the same retrieved materials, and a
comparison run (--answer-model) varies only the answer model.
"""

import argparse
import asyncio
from collections import Counter
from contextlib import asynccontextmanager, contextmanager, nullcontext
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
import json
import math
import os
from pathlib import Path
import re
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import psycopg
from psycopg import sql

from backend import dart_rag, llm, price_card, stock_analysis
from backend.broker import Balance, DailyBar, FiveDayFlow, Holding, InvestorFlow, Quote
from backend.dart_etl import INSERT_CHUNK
from backend.finance_calc import ANNUAL, THIRD, LockupRelease, calculate_scorecard, simulate_purchase
from backend.main import chat_events
from backend.market_time import KST
from backend.portfolio_graph import build_graph
from backend.portfolio_market import ScoreInputs
from backend.serialization import dump
from backend.sync_stock_master import Stock


ROOT = Path(__file__).resolve().parents[1]
DATASET = ROOT / "data/eval/ragas_benchmark_20.json"
AS_OF = date(2026, 4, 30)
# After the 15:40 KRX confirmation, so the card's session is AS_OF and its DART window ends there.
CARD_CLOCK = datetime(2026, 4, 30, 18, tzinfo=KST)
TAGGED = re.compile(r"\s*\[(?:DOC|NEWS|P)-[0-9]+\]")
FALLBACK_WARNING = "답변 모델을 사용할 수 없어"
EVENT_TYPES = ("bond_issue", "rights_issue", "control_change", "supply_contract", "preliminary_earnings")
TARGETS = {"faithfulness": 0.90, "context_precision": 0.85, "numeric_error_pct": 0.0}
METRIC_LABELS = {
    "faithfulness": "Faithfulness",
    "context_precision": "Context Precision",
    "numeric_error_pct": "정형 수치 오차율 (%)",
}


def load_dataset(path):
    dataset = json.loads(path.read_text(encoding="utf-8"))
    cases = dataset["cases"]
    if Counter(case["type"] for case in cases) != dict.fromkeys(EVENT_TYPES, 4):
        raise ValueError("Expected four cases for each of the five disclosure types")
    for field in ("id", "stock_code", "corp_code"):
        if len({case[field] for case in cases}) != 20:
            raise ValueError(f"Expected 20 distinct {field} values")
    chunks = set()
    for case in cases:
        if not case["question"].strip() or not case["reference"].strip():
            raise ValueError(f"{case['id']}: empty question or reference")
        if not re.fullmatch(r"[0-9]{8}", case["corp_code"]) or not re.fullmatch(r"[0-9]{6}", case["stock_code"]):
            raise ValueError(f"{case['id']}: invalid issuer or stock code")
        if {doc["relevant"] for doc in case["documents"]} != {True, False}:
            raise ValueError(f"{case['id']}: supply relevant and irrelevant documents")
        for doc in case["documents"]:
            receipt = doc["rcept_no"]
            key = (receipt, doc["chunk_no"])
            if (not re.fullmatch(r"[0-9]{14}", receipt) or key in chunks or doc["chunk_no"] < 0
                    or not doc["text"].strip() or not doc["title"].strip()):
                raise ValueError(f"{case['id']}: invalid or duplicate document")
            date.fromisoformat(receipt[:8])
            chunks.add(key)
    return dataset


def scenario(case):
    """Inject a fixed account and market snapshot while running the production graph."""
    values = {key: Decimal(value) for key, value in case["account"].items()}
    code, corp, name = case["stock_code"], case["corp_code"], case["issuer"]
    stock = Stock(code, corp, f"KR7{code}001", None, name, "STOCK", "26", values["listed_shares"])
    qty = Decimal(10)
    average, price, budget = (values[key] for key in ("average_price", "current_price", "purchase_budget"))
    cost, market_value, cash = average * qty, price * qty, budget * 2
    profit = market_value - cost
    total = market_value + cash
    holding = Holding(code, name, "STOCK", corp, stock.isin_cd, qty, average, price,
                      cost, market_value, profit, profit * 100 / cost, market_value / total)
    balance = Balance(cash, total, cash / total, (holding,))
    days = tuple(InvestorFlow(AS_OF - timedelta(days=4 - i), Decimal(10), Decimal(-1)) for i in range(5))
    flow = FiveDayFlow(days, Decimal(50), Decimal(-5))
    close = price - 100
    bars = tuple(DailyBar(AS_OF - timedelta(days=21 - i), close, close, close, close, Decimal(100))
                 for i in range(21))
    earnings = {"corp_code": corp, "fs_div": "CFS", "sj_div": "IS", "account_id": "dart_OperatingIncomeLoss"}
    q3, q4 = values["q3_profit"], values["q4_profit"]
    reports = {
        (AS_OF.year - 1, ANNUAL, "CFS"): {"status": "000", "list": [dict(earnings, thstrm_amount=q3 + q4)]},
        (AS_OF.year - 1, THIRD, "CFS"): {
            "status": "000", "list": [dict(earnings, thstrm_amount=q3, thstrm_add_amount=q3)],
        },
    }
    events = []
    if values["convertible_shares"]:
        events = [{"corp_code": corp, "convertible_shares": values["convertible_shares"],
                   "board_resolution_date": AS_OF, "rcept_no": next(
                       doc["rcept_no"] for doc in case["documents"] if doc["relevant"])}]
    releases = [LockupRelease(stock.isin_cd, AS_OF + timedelta(days=10), values["lockup_shares"])]
    inputs = ScoreInputs(stock, flow, bars, reports, releases, events)

    async def collect():
        return balance, AS_OF, None, {code: inputs}, []

    async def orderable_cash(_holding):
        return cash

    async def value(result=None):
        return result

    # What price_card.get_or_build_card reads: quote, bars, flow and balance, an empty cache,
    # ETL that reports indexed chunks (dart_rag.search serves them) and no news.
    broker = SimpleNamespace(
        clock=lambda: CARD_CLOCK,
        get_quote=lambda *_, **__: value(Quote(code, price, "eval", AS_OF, Decimal(0))),
        get_daily_bars=lambda *_, **__: value(bars),
        get_five_day_flow=lambda *_, **__: value(flow),
        get_balance=lambda: value(balance),
        redis=SimpleNamespace(get=lambda _key: value(), set=lambda *_, **__: value()),
    )
    etl = SimpleNamespace(ingest_missing_stock=lambda *_: value(), has_recent_chunks=lambda *_: value(True))
    market = SimpleNamespace(collect=collect, orderable_cash=orderable_cash, broker=broker, etl=etl,
                             external=SimpleNamespace(get_news=lambda *_, **__: value([])))
    card = calculate_scorecard(stock, holding, flow, as_of=AS_OF, reports=reports, bars=bars,
                               releases=releases, overhang_events=events)
    purchase = simulate_purchase(holding, balance, cash, amount=budget)
    expected = {
        "dilution_pct": card["dilution_pct"],
        # diagnosis_report renders prices as whole won, rounded half up.
        "average_price": holding.average_price.to_integral_value(rounding=ROUND_HALF_UP),
        "simulated_average_price": purchase.average_price.to_integral_value(rounding=ROUND_HALF_UP),
        "scorecard": Decimal(card["total"]),
    }
    query = f"{case['question']} {code} 종목을 {budget:f}원 추매하면 예상 평단가는 얼마인가요?"
    return build_graph(market), query, expected, stock


NUMBER = r"([+-]?(?:[0-9]{1,3}(?:,[0-9]{3})+|[0-9]+)(?:\.[0-9]+)?)"
NUMERIC_PATTERNS = {
    "dilution_pct": rf" · 오버행 희석률 {NUMBER}%$",
    "average_price": rf", 평단가 {NUMBER}원",
    "simulated_average_price": rf"^- 매수 시뮬레이션 · .*, 예상 평단가 {NUMBER}원$",
    "scorecard": rf" \(합계 {NUMBER}/100\)$",
}


def grade_numbers(report, name, expected):
    """Require one correctly rendered value per field in the deterministic evidence section."""
    answer, _, evidence = report.partition(f"\n{stock_analysis.EVIDENCE_HEADING}\n")
    holding = "\n".join(line for line in evidence.splitlines() if line.startswith(f"- {name} · "))
    simulations = re.findall(r"^- 매수 시뮬레이션 · ", evidence, re.MULTILINE)
    # The local report names the stock in its answer text; the answer report in its evidence line.
    simulated_name = set(re.findall(r"\*\*(.+?)\*\* 현재가에 [0-9,]+주 체결을 가정하면", answer))
    simulated_name |= set(re.findall(r"^- 매수 시뮬레이션 · ([^·\n]+?) · 투자 금액", evidence, re.MULTILINE))
    checks = {}
    for field, target in expected.items():
        is_simulation = field == "simulated_average_price"
        text = evidence if is_simulation else holding
        matches = [Decimal(value.replace(",", ""))
                   for value in re.findall(NUMERIC_PATTERNS[field], text, re.MULTILINE)]
        correct_scope = len(simulations) == 1 and simulated_name == {name} if is_simulation else True
        checks[field] = {"expected": format(target, "f"), "observed": [format(v, "f") for v in matches],
                         "passed": matches == [target] and correct_scope}
    return checks


def disclosure_docs(context):
    """The DOC chunks (with text) the answer model received, in the order it received them."""
    return [doc for card in context.get("cards", []) for doc in card["docs"] if doc.get("text")]


def dry_answer(case, context):
    """An extractive answer: each relevant chunk the context carries, cited by its [DOC-n] tag."""
    relevant = {doc["text"] for doc in case["documents"] if doc["relevant"]}
    cited = [doc for doc in disclosure_docs(context) if doc["text"] in relevant]
    core = " ".join(f"{doc['text'].rstrip('.')} [{doc['id']}]." for doc in cited)
    tags = list(dict.fromkeys(doc["id"] for doc in cited))
    evidence = "\n".join(f"- [{tag}] 공시 발췌" for tag in tags)
    return stock_analysis.Analysis(
        analysis_text=f"{stock_analysis.ANSWER_HEADING}\n\n{core}\n\n{stock_analysis.EVIDENCE_HEADING}\n\n{evidence}",
        citations=tags)


def core_answer(report):
    """The report's core answer section without citation tags, the text Faithfulness judges."""
    answer = report.partition(f"\n{stock_analysis.EVIDENCE_HEADING}\n")[0]
    return TAGGED.sub("", answer.removeprefix(stock_analysis.ANSWER_HEADING)).strip()


async def run_case(case, *, dry_run):
    """Stream one case through the production graph.

    Returns (answer context, core answer, checks, local-report fallback, provider fallback). The
    provider fallback is True when a non-Gemini answer model was requested but llm.invoke answered
    with Gemini (missing DashScope key, 429/403, timeout or 5xx), so the answer is not that model's.
    """
    graph, query, expected, stock = scenario(case)
    production_invoke, production_gemini = llm.invoke, llm.invoke_gemini
    context, gemini_roles = {}, []

    async def invoke_gemini(role, *args, **kwargs):
        gemini_roles.append(role)
        return await production_gemini(role, *args, **kwargs)

    async def invoke(role, instruction, prompt, schema, **kwargs):
        if role == "answer":
            context.clear()
            context.update(json.loads(prompt))
            if dry_run:
                return dry_answer(case, context)
            return await production_invoke(role, instruction, prompt, schema, **kwargs)
        # The analyst is blocked in both modes: cards are local, so only the answer model varies.
        raise llm.LLMUnavailable("Evaluation keeps price cards local", reason="평가용 로컬 카드")

    tokens = []
    done = None
    with (patch.object(llm, "invoke", invoke), patch.object(llm, "invoke_gemini", invoke_gemini),
          patch.object(price_card, "fetch_stocks", AsyncMock(return_value={stock.stock_code: stock})),
          patch.object(price_card, "record_card", AsyncMock())):
        async for frame in chat_events(graph, query):
            event, data, *_ = frame.splitlines()
            payload = json.loads(data.removeprefix("data: "))
            if event == "event: token":
                tokens.append(payload["text"])
            elif event == "event: done":
                done = payload
    if not done or not done["ok"] or done["intent_type"] != "simulation" or not context:
        raise RuntimeError(f"{case['id']}: analysis did not complete")
    report = done["final_report"]
    if "".join(tokens) != report:
        raise RuntimeError(f"{case['id']}: token stream differs from final report")
    fallback = any(warning.startswith(FALLBACK_WARNING) for warning in done["guardrail_warnings"])
    if fallback and dry_run:
        raise RuntimeError(f"{case['id']}: the dry answer did not pass the answer verifier")
    # A failed answer model leaves the local report; there is no model answer to judge.
    answer = "" if fallback else core_answer(report)
    provider_fallback = not llm.model_name("answer").startswith("gemini") and "answer" in gemini_roles
    return context, answer, grade_numbers(report, case["issuer"], expected), fallback, provider_fallback


@contextmanager
def fixed_retrieval(case):
    """Serve the case's chunks from dart_rag.search in dataset order (no database or GPU)."""
    rows = [{"rcept_no": doc["rcept_no"], "report_nm": doc["title"], "chunk_text": doc["text"]}
            for doc in case["documents"]]

    async def search(corp_code, _query, *, limit=5, since=None, until=None, **_):
        if corp_code != case["corp_code"]:
            raise ValueError("Unexpected issuer")
        # The fixed rank keeps dataset order; the card's DART window applies as in production.
        return [row for row in rows
                if (since or date.min) <= date.fromisoformat(row["rcept_no"][:8]) <= (until or date.max)][:limit]

    with patch.object(dart_rag, "search", search):
        yield


async def dry_retrieval(case):
    with fixed_retrieval(case):
        return await run_case(case, dry_run=True)


def dry_scores(case, context, answer):
    retrieved = [doc["text"] for doc in disclosure_docs(context)]
    relevant = {doc["text"] for doc in case["documents"] if doc["relevant"]}
    hits = [index for index, text in enumerate(retrieved, 1) if text in relevant]
    precision = sum(count / rank for count, rank in enumerate(hits, 1)) / len(hits) if hits else 0.0
    # The dry answer is extractive. This check is deterministic and independent of a judge.
    claims = re.split(r"(?<=\.)\s+", answer) if answer else []
    faithful = sum(claim in retrieved for claim in claims) / len(claims) if claims else 0.0
    return faithful, precision


@asynccontextmanager
async def indexed_corpus(cases):
    """Isolate the benchmark corpus in a temporary schema on the configured pgvector database."""
    name = "ragas_eval_" + uuid4().hex
    schema = sql.Identifier(name)
    with psycopg.connect(autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE SCHEMA {}").format(schema))
        try:
            conn.execute(sql.SQL("SET search_path TO {}, public").format(schema))
            conn.execute((ROOT / "backend/migrations/004_module4.sql").read_text(encoding="utf-8"))
            # The unqualified table in dart_rag.search resolves to this schema for this CLI process.
            options = f"{os.getenv('PGOPTIONS', '')} -c search_path={name},public".strip()
            with patch.dict(os.environ, {"PGOPTIONS": options}):
                with conn.transaction(), conn.cursor() as cur:
                    for case in cases:
                        docs = case["documents"]
                        texts = [doc["text"] for doc in docs]
                        for start in range(0, len(docs), dart_rag.BATCH_SIZE):
                            batch = docs[start:start + dart_rag.BATCH_SIZE]
                            vectors = await dart_rag.embed(texts[start:start + dart_rag.BATCH_SIZE])
                            for doc, vector in zip(batch, vectors, strict=True):
                                tokens = await dart_rag.keywords(doc["text"])
                                receipt = doc["rcept_no"]
                                cur.execute(INSERT_CHUNK, (case["corp_code"], receipt, doc["chunk_no"], doc["title"],
                                                          date.fromisoformat(receipt[:8]), doc["text"],
                                                          json.dumps(vector, allow_nan=False), " ".join(tokens)))
                yield
        finally:
            conn.rollback()
            conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(schema))


async def evaluate(dataset, *, dry_run, judge_model):
    results = []
    models = {"answer_model": llm.model_name("answer"), "judge_model": judge_model}
    if dry_run:
        for case in dataset["cases"]:
            context, answer, checks, *fallbacks = await dry_retrieval(case)
            results.append(case_result(case, *dry_scores(case, context, answer), checks, *fallbacks))
        return {**summarize(dataset, results, "dry-run"), **models}

    from ragas.dataset_schema import SingleTurnSample
    from ragas.llms import LangchainLLMWrapper
    from ragas.metrics import Faithfulness, LLMContextPrecisionWithReference

    judge = LangchainLLMWrapper(llm.gemini(judge_model), bypass_n=True)
    faithful = Faithfulness(llm=judge)
    precision = LLMContextPrecisionWithReference(llm=judge)
    async with indexed_corpus(dataset["cases"]):
        for case in dataset["cases"]:
            context, answer, checks, *fallbacks = await run_case(case, dry_run=False)
            disclosures = [f"{doc['title']}\n{doc['text']}" for doc in disclosure_docs(context)]
            # The rest of the answer context (account, levels, facts, simulation) also grounds claims.
            indicators = {**context, "cards": [{key: value for key, value in card.items() if key != "docs"}
                                               for card in context.get("cards", [])]}
            sample = SingleTurnSample(user_input=case["question"], response=answer, reference=case["reference"],
                                      retrieved_contexts=disclosures)
            f = await faithful.single_turn_ascore(sample.model_copy(update={
                "retrieved_contexts": [*disclosures, dump(indicators)]})) if answer else 0.0
            p = await precision.single_turn_ascore(sample) if disclosures else 0.0
            results.append(case_result(case, f, p, checks, *fallbacks))
    return {**summarize(dataset, results, "live"), **models}


def case_result(case, faithfulness, precision, checks, answer_fallback=False, provider_fallback=False):
    if not all(math.isfinite(x) and 0 <= x <= 1 for x in (faithfulness, precision)):
        raise ValueError(f"{case['id']}: judge returned an invalid score")
    return {"id": case["id"], "type": case["type"], "faithfulness": faithfulness,
            "context_precision": precision, "numeric_checks": checks, "answer_fallback": answer_fallback,
            "provider_fallback": provider_fallback}


def summarize(dataset, results, mode):
    checks = [check for case in results for check in case["numeric_checks"].values()]
    failures = sum(not check["passed"] for check in checks)
    fallbacks = sum(case["answer_fallback"] for case in results)
    # A Gemini answer under a Qwen label would make the model comparison meaningless.
    providers = sum(case["provider_fallback"] for case in results)
    metrics = {key: sum(case[key] for case in results) / len(results)
               for key in ("faithfulness", "context_precision")}
    metrics["numeric_error_pct"] = float(Decimal(failures) * 100 / len(checks))
    passed = all(metrics[key] >= TARGETS[key] for key in ("faithfulness", "context_precision"))
    return {"mode": mode, "dataset_source": dataset["source"], "case_count": len(results), "metrics": metrics,
            "targets": TARGETS, "numeric_checks": len(checks), "numeric_failures": failures,
            "answer_failures": fallbacks, "provider_fallbacks": providers,
            "passed": passed and failures == 0 and fallbacks == 0 and providers == 0, "cases": results}


def markdown(report):
    lines = [f"RAG 평가 · {report['mode']} · {report['dataset_source']} · {report['case_count']}건",
             "", "| 지표 | 결과 | 목표 |", "| --- | ---: | ---: |"]
    for key, target in TARGETS.items():
        operator = "=" if key == "numeric_error_pct" else "≥"
        lines.append(f"| {METRIC_LABELS[key]} | {report['metrics'][key]:.4f} | {operator} {target:.2f} |")
    lines.extend(["", f"정형 수치 검사 {report['numeric_checks']}건 중 오류 {report['numeric_failures']}건",
                  f"답변 모델 실패(로컬 리포트 대체) {report['answer_failures']}건",
                  f"대체 모델(Gemini) 응답 {report['provider_fallbacks']}건"])
    if "answer_model" in report:
        lines.append(f"답변 모델 {report['answer_model']} · Judge {report['judge_model']}")
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=DATASET)
    parser.add_argument("--dry-run", action="store_true", help="Use offline fixture retrieval and scoring")
    parser.add_argument("--answer-model", help="ANSWER_MODEL for this run only, e.g. qwen3.8-flash or gemini-3.8-flash")
    parser.add_argument("--judge-model", default=llm.model_name("judge"))
    parser.add_argument("--format", choices=("json", "markdown"), default="json")
    args = parser.parse_args(argv)
    if not args.dry_run and not os.getenv("GEMINI_API_KEY", "").strip():
        parser.error("GEMINI_API_KEY is required for live evaluation")
    answer_model = args.answer_model or llm.model_name("answer")
    if not args.dry_run and not answer_model.startswith("gemini") and not os.getenv("DASHSCOPE_API_KEY", "").strip():
        parser.error(f"실측 평가에서 {answer_model} 답변 모델을 쓰려면 DASHSCOPE_API_KEY가 필요합니다. "
                     "키가 없으면 Gemini 대체 응답이 이 모델의 결과로 기록됩니다.")
    policy = asyncio.SelectorEventLoop if sys.platform == "win32" else None
    # patch.dict restores ANSWER_MODEL (or its absence) when the run ends.
    override = patch.dict(os.environ, {"ANSWER_MODEL": args.answer_model}) if args.answer_model else nullcontext()
    try:
        with override, asyncio.Runner(loop_factory=policy) as runner:
            report = runner.run(evaluate(load_dataset(args.dataset), dry_run=args.dry_run, judge_model=args.judge_model))
    except ImportError as exc:
        parser.exit(2, f"Evaluation dependency is missing: {exc.name}. See scripts/requirements-eval.txt.\n")
    except Exception as exc:
        # Provider and DB exceptions can contain credential-bearing URLs.
        parser.exit(2, f"Evaluation could not complete ({type(exc).__name__}). Check dataset and runtime configuration.\n")
    print(markdown(report) if args.format == "markdown" else json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
