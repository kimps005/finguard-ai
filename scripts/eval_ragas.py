"""Evaluate issuer retrieval, stock analysis and the streamed portfolio report."""

import argparse
import asyncio
from collections import Counter
from contextlib import asynccontextmanager, nullcontext
from datetime import date, timedelta
from decimal import Decimal
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

from backend import dart_rag, llm, stock_analysis
from backend.broker import Balance, DailyBar, FiveDayFlow, Holding, InvestorFlow
from backend.dart_etl import INSERT_CHUNK
from backend.finance_calc import ANNUAL, THIRD, LockupRelease, calculate_scorecard, simulate_purchase
from backend.main import chat_events
from backend.portfolio_graph import build_graph
from backend.portfolio_market import ScoreInputs
from backend.sync_stock_master import Stock


ROOT = Path(__file__).resolve().parents[1]
DATASET = ROOT / "data/eval/ragas_benchmark_20.json"
AS_OF = date(2026, 4, 30)
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
        return balance, AS_OF, None, {code: inputs}

    async def orderable_cash(_holding):
        return cash

    async def get_news(_name):
        return []

    market = SimpleNamespace(collect=collect, orderable_cash=orderable_cash,
                             external=SimpleNamespace(get_news=get_news))
    card = calculate_scorecard(stock, holding, flow, as_of=AS_OF, reports=reports, bars=bars,
                               releases=releases, overhang_events=events)
    purchase = simulate_purchase(holding, balance, cash, amount=budget)
    expected = {
        "dilution_pct": card["dilution_pct"],
        "average_price": holding.average_price,
        # report_parts displays the simulated average rounded to two decimal places.
        "simulated_average_price": Decimal(f"{purchase.average_price:.2f}"),
        "scorecard": Decimal(card["total"]),
    }
    query = f"{case['question']} {code} 종목을 {budget:f}원 추매하면 예상 평단가는 얼마인가요?"
    return build_graph(market), query, expected


NUMBER = r"([+-]?(?:[0-9]{1,3}(?:,[0-9]{3})+|[0-9]+)(?:\.[0-9]+)?)"
NUMERIC_PATTERNS = {
    "dilution_pct": rf" · 오버행 희석률 {NUMBER}%",
    "average_price": rf" · 평단가 {NUMBER}원",
    "simulated_average_price": rf"^\| 예상 평단가 \| {NUMBER}원 \|$",
    "scorecard": rf" · 정량 점수 {NUMBER}/100(?![\d.])",
}


def grade_numbers(report, stock_code, expected):
    """Require one correctly rendered value per field for the evaluated holding."""
    holding_lines = [line for line in report.splitlines()
                     if re.match(rf"^- .+ \({re.escape(stock_code)}\) · ", line)]
    holding = "\n".join(holding_lines)
    # These fields are emitted before the first worker's Markdown section.
    before_workers = re.split(r"^### [0-9]{6}$", report, maxsplit=1, flags=re.MULTILINE)[0]
    simulations = before_workers.split("\n### 매수 시뮬레이션\n")
    table = simulations[-1] if len(simulations) == 2 else ""
    simulation_codes = re.findall(r"^\| 종목 \| ([0-9]{6}) \|$", table, re.MULTILINE)
    checks = {}
    for field, target in expected.items():
        is_simulation = field == "simulated_average_price"
        text = table if is_simulation else holding
        matches = [Decimal(value.replace(",", ""))
                   for value in re.findall(NUMERIC_PATTERNS[field], text, re.MULTILINE)]
        correct_scope = simulation_codes == [stock_code] if is_simulation else len(holding_lines) == 1
        checks[field] = {"expected": format(target, "f"), "observed": [format(v, "f") for v in matches],
                         "passed": matches == [target] and correct_scope}
    return checks


async def run_case(case, *, dry_run):
    graph, query, expected = scenario(case)
    production_analyze = stock_analysis.analyze
    context = {}

    async def capture(prompt):
        context.update(json.loads(prompt))
        if not dry_run:
            return await production_analyze(prompt)
        relevant = {doc["text"] for doc in case["documents"] if doc["relevant"]}
        matched = [doc for doc in context["disclosures"] if doc["text"] in relevant]
        return {"analysis_text": " ".join(
            f"{doc['text'].rstrip('.')} [{doc['source']}]." for doc in matched),
            "citations": list(dict.fromkeys(doc["source"] for doc in matched))}

    tokens = []
    done = None
    # Exercise local fallback without using any configured cloud credentials in a dry run.
    offline = patch.object(llm, "invoke", AsyncMock(side_effect=llm.LLMUnavailable)) if dry_run else nullcontext()
    with offline, patch.object(stock_analysis, "analyze", capture):
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
    answer = " ".join(row["analysis_text"] for row in done["verified_stock_results"])
    answer = re.sub(r"\s*\[[0-9]{14}\]", "", answer)
    return context, answer, grade_numbers(report, case["stock_code"], expected)


async def dry_retrieval(case):
    rows = [{"rcept_no": doc["rcept_no"], "report_nm": doc["title"], "chunk_text": doc["text"]}
            for doc in case["documents"]]

    async def search(corp_code, _query):
        if corp_code != case["corp_code"]:
            raise ValueError("Unexpected issuer")
        return rows

    with patch.object(dart_rag, "search", search):
        context, answer, checks = await run_case(case, dry_run=True)
    return context, answer, checks


def dry_scores(case, context, answer):
    retrieved = [doc["text"] for doc in context["disclosures"]]
    relevant = {doc["text"] for doc in case["documents"] if doc["relevant"]}
    hits = [index for index, text in enumerate(retrieved, 1) if text in relevant]
    precision = sum(count / rank for count, rank in enumerate(hits, 1)) / len(hits) if hits else 0.0
    # The dry worker produces extractive claims. This check is deterministic and independent of a judge.
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
    if dry_run:
        for case in dataset["cases"]:
            context, answer, checks = await dry_retrieval(case)
            results.append(case_result(case, *dry_scores(case, context, answer), checks))
        return summarize(dataset, results, "dry-run")

    from ragas.dataset_schema import SingleTurnSample
    from ragas.llms import LangchainLLMWrapper
    from ragas.metrics import Faithfulness, LLMContextPrecisionWithReference

    judge = LangchainLLMWrapper(llm.gemini(judge_model), bypass_n=True)
    faithful = Faithfulness(llm=judge)
    precision = LLMContextPrecisionWithReference(llm=judge)
    async with indexed_corpus(dataset["cases"]):
        for case in dataset["cases"]:
            context, answer, checks = await run_case(case, dry_run=False)
            disclosures = [f"{doc['title']}\n{doc['text']}" for doc in context["disclosures"]]
            indicators = {k: v for k, v in context.items() if k != "disclosures"}
            sample = SingleTurnSample(user_input=case["question"], response=answer, reference=case["reference"],
                                      retrieved_contexts=disclosures)
            f = await faithful.single_turn_ascore(sample.model_copy(update={
                "retrieved_contexts": [*disclosures, stock_analysis.dump(indicators)]})) if answer else 0.0
            p = await precision.single_turn_ascore(sample) if disclosures else 0.0
            results.append(case_result(case, f, p, checks))
    return {**summarize(dataset, results, "live"), "judge_model": judge_model,
            "primary_worker_model": llm.model_name("worker")}


def case_result(case, faithfulness, precision, checks):
    if not all(math.isfinite(x) and 0 <= x <= 1 for x in (faithfulness, precision)):
        raise ValueError(f"{case['id']}: judge returned an invalid score")
    return {"id": case["id"], "type": case["type"], "faithfulness": faithfulness,
            "context_precision": precision, "numeric_checks": checks}


def summarize(dataset, results, mode):
    checks = [check for case in results for check in case["numeric_checks"].values()]
    failures = sum(not check["passed"] for check in checks)
    metrics = {key: sum(case[key] for case in results) / len(results)
               for key in ("faithfulness", "context_precision")}
    metrics["numeric_error_pct"] = float(Decimal(failures) * 100 / len(checks))
    passed = all(metrics[key] >= TARGETS[key] for key in ("faithfulness", "context_precision"))
    return {"mode": mode, "dataset_source": dataset["source"], "case_count": len(results), "metrics": metrics,
            "targets": TARGETS, "numeric_checks": len(checks), "numeric_failures": failures,
            "passed": passed and failures == 0, "cases": results}


def markdown(report):
    lines = [f"RAG 평가 · {report['mode']} · {report['dataset_source']} · {report['case_count']}건",
             "", "| 지표 | 결과 | 목표 |", "| --- | ---: | ---: |"]
    for key, target in TARGETS.items():
        operator = "=" if key == "numeric_error_pct" else "≥"
        lines.append(f"| {METRIC_LABELS[key]} | {report['metrics'][key]:.4f} | {operator} {target:.2f} |")
    lines.extend(["", f"정형 수치 검사 {report['numeric_checks']}건 중 오류 {report['numeric_failures']}건"])
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=DATASET)
    parser.add_argument("--dry-run", action="store_true", help="Use offline fixture retrieval and scoring")
    parser.add_argument("--judge-model", default=llm.model_name("synthesizer"))
    parser.add_argument("--format", choices=("json", "markdown"), default="json")
    args = parser.parse_args()
    if not args.dry_run and not os.getenv("GEMINI_API_KEY", "").strip():
        parser.error("GEMINI_API_KEY is required for live evaluation")
    policy = asyncio.SelectorEventLoop if sys.platform == "win32" else None
    try:
        with asyncio.Runner(loop_factory=policy) as runner:
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
