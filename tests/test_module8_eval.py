import asyncio
from collections import Counter
from decimal import Decimal
import json
from unittest.mock import AsyncMock

import httpx
import pytest

from scripts import benchmark_latency as latency
from scripts import eval_ragas as ragas


def clear_model_env(monkeypatch):
    # The shell's model choice must not leak into the recorded models.
    for name in ("ANSWER_MODEL", "JUDGE_MODEL", "FALLBACK_MODEL"):
        monkeypatch.delenv(name, raising=False)


def test_balanced_offline_evaluation_uses_the_graph_and_all_numeric_fields(monkeypatch):
    clear_model_env(monkeypatch)
    dataset = ragas.load_dataset(ragas.DATASET)
    report = asyncio.run(ragas.evaluate(dataset, dry_run=True, judge_model="unused"))
    assert Counter(row["type"] for row in report["cases"]) == dict.fromkeys(ragas.EVENT_TYPES, 4)
    assert report["dataset_source"] == "OpenDART"
    assert report["mode"] == "dry-run"
    assert report["passed"] is True
    assert report["metrics"] == {"faithfulness": 1.0, "context_precision": 1.0, "numeric_error_pct": 0.0}
    assert report["numeric_checks"] == 80
    assert report["numeric_failures"] == 0
    assert report["cases"][0]["numeric_checks"] == {
        "dilution_pct": {"expected": "3.25", "observed": ["3.25"], "passed": True},
        "average_price": {"expected": "9637", "observed": ["9637"], "passed": True},
        "simulated_average_price": {"expected": "10073", "observed": ["10073"], "passed": True},
        "scorecard": {"expected": "60", "observed": ["60"], "passed": True},
    }
    assert "| Faithfulness | 1.0000 | ≥ 0.90 |" in ragas.markdown(report)
    assert report["answer_failures"] == 0
    assert report["answer_model"] == "qwen3.8-flash" and report["judge_model"] == "unused"


def test_answer_model_flag_is_recorded(monkeypatch, capsys):
    clear_model_env(monkeypatch)
    monkeypatch.setenv("ANSWER_MODEL", "qwen3.8-flash")
    seen = []
    production = ragas.evaluate

    async def evaluate(*args, **kwargs):
        seen.append(ragas.llm.model_name("answer"))
        return await production(*args, **kwargs)

    monkeypatch.setattr(ragas, "evaluate", evaluate)
    assert ragas.main(["--dry-run", "--answer-model", "gemini-3.8-flash"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["answer_model"] == "gemini-3.8-flash" and seen == ["gemini-3.8-flash"]
    assert report["judge_model"] == "gemini-3.8-flash"
    assert report["passed"] is True and report["numeric_failures"] == 0
    # The override lasts only for the run.
    assert ragas.os.environ["ANSWER_MODEL"] == "qwen3.8-flash"


@pytest.mark.parametrize("argv", [[], ["--answer-model", "qwen3.8-flash"]])
def test_live_qwen_comparison_requires_the_dashscope_key(monkeypatch, capsys, argv):
    clear_model_env(monkeypatch)
    monkeypatch.setenv("GEMINI_API_KEY", "gemini-test")
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    monkeypatch.setattr(ragas, "evaluate", pytest.fail)  # nothing may run
    with pytest.raises(SystemExit) as exit_:
        ragas.main(argv)
    assert exit_.value.code == 2
    assert "qwen3.8-flash 답변 모델을 쓰려면 DASHSCOPE_API_KEY가 필요합니다" in capsys.readouterr().err


def test_gemini_answer_under_a_qwen_label_is_a_provider_fallback(monkeypatch):
    clear_model_env(monkeypatch)
    monkeypatch.setenv("DASHSCOPE_API_KEY", "dash-test")
    monkeypatch.setenv("GEMINI_API_KEY", "gemini-test")
    case = ragas.load_dataset(ragas.DATASET)["cases"][0]
    quota = httpx.HTTPStatusError("quota", request=httpx.Request("POST", "https://test"),
                                  response=httpx.Response(429))
    dashscope = AsyncMock(side_effect=quota)
    # DashScope says 429, so llm.invoke answers with Gemini; no network call is made.
    gemini = AsyncMock(side_effect=lambda role, model, messages, schema, **_: ragas.dry_answer(
        case, json.loads(messages[1][1])))
    monkeypatch.setattr(ragas.llm, "invoke_dashscope", dashscope)
    monkeypatch.setattr(ragas.llm, "invoke_gemini", gemini)

    async def run():
        with ragas.fixed_retrieval(case):
            return await ragas.run_case(case, dry_run=False)

    context, answer, checks, fallback, provider_fallback = asyncio.run(run())
    assert dashscope.await_count == 1 and gemini.await_args.args[:2] == ("answer", "gemini-3.8-flash")
    assert answer and fallback is False and provider_fallback is True
    summary = ragas.summarize({"source": "test"}, [ragas.case_result(
        case, 1.0, 1.0, checks, fallback, provider_fallback)], "live")
    assert summary["provider_fallbacks"] == 1 and summary["numeric_failures"] == 0
    assert summary["passed"] is False
    assert "대체 모델(Gemini) 응답 1건" in ragas.markdown(summary)


@pytest.mark.parametrize("original,replacement,failed_field", [
    ("3.25%", "3.24%", "dilution_pct"),
    ("- 가상 · 오버행 희석률 3.25%\n", "", "dilution_pct"),
    ("평단가 9,637원", "평단가 9,638원", "average_price"),
    ("(합계 60/100)", "(합계 60/95)", "scorecard"),
    ("(합계 60/100)", "(합계 60/100)\n- 가상 · 수급 0, 모멘텀 0, 실적 0, 오버행 0점 (합계 59/100)", "scorecard"),
    ("예상 평단가 10,073원\n", "예상 평단가 10,074원\n", "simulated_average_price"),
    ("**가상** 현재가에", "**다른종목** 현재가에", "simulated_average_price"),
    ("- 매수 시뮬레이션 · 투자", "- 매수 시뮬레이션 · 다른종목 · 투자", "simulated_average_price"),
])
def test_numeric_grading_counts_missing_wrong_and_duplicate_values(original, replacement, failed_field):
    # Values in the answer prose and in other holdings' lines must not count.
    report = ("### 핵심 답변\n\n**가상** 현재가에 2주 체결을 가정하면 예상 평단가는 10,074원입니다. "
              "합산 점수는 59/100입니다.\n\n### 판단 근거\n\n"
              "- 매수 시뮬레이션 · 투자 금액 20,146원, 매수 수량 2주, 예상 평단가 10,073원\n"
              "- 가상 · 현재가 12,253원, 평단가 9,637원, 20일선 12,153원\n"
              "- 가상 · 오버행 희석률 3.25%\n"
              "- 가상 · 수급 15, 모멘텀 15, 실적 25, 오버행 5점 (합계 60/100)\n"
              "- 다른종목 · 수급 25, 모멘텀 25, 실적 25, 오버행 5점 (합계 80/100)")
    expected = dict(zip(ragas.NUMERIC_PATTERNS, map(Decimal, ("3.25", "9637", "10073", "60")), strict=True))
    checks = ragas.grade_numbers(report.replace(original, replacement), "가상", expected)
    assert [name for name, check in checks.items() if not check["passed"]] == [failed_field]
    result = ragas.case_result({"id": "test", "type": "bond_issue"}, 1.0, 1.0, checks)
    summary = ragas.summarize({"source": "test"}, [result], "dry-run")
    assert summary["metrics"]["numeric_error_pct"] == 25.0
    assert summary["passed"] is False


def test_simulation_attributed_by_the_answer_report_evidence_line():
    # The answer model's prose need not name the simulated stock; the local evidence line does.
    report = ("### 핵심 답변\n\n공시상 전환 물량이 있습니다.\n\n### 판단 근거\n\n"
              "- 매수 시뮬레이션 · 가상 · 투자 금액 20,146원, 매수 수량 2주, 예상 평단가 10,073원\n"
              "- 가상 · 현재가 12,253원, 평단가 9,637원, 20일선 12,153원\n"
              "- 가상 · 오버행 희석률 3.25%\n"
              "- 가상 · 수급 15, 모멘텀 15, 실적 25, 오버행 5점 (합계 60/100)")
    expected = dict(zip(ragas.NUMERIC_PATTERNS, map(Decimal, ("3.25", "9637", "10073", "60")), strict=True))
    assert all(check["passed"] for check in ragas.grade_numbers(report, "가상", expected).values())
    prose = report.replace("공시상", "**다른종목** 현재가에 2주 체결을 가정하면 공시상")
    assert not ragas.grade_numbers(prose, "가상", expected)["simulated_average_price"]["passed"]


def test_dry_rank_and_grounding_penalize_misordered_context_and_unsupported_answer():
    case = {"documents": [{"text": "근거.", "relevant": True}]}
    context = {"cards": [{"docs": [{"id": "DOC-1", "title": "무관", "text": "무관."},
                                   {"id": "DOC-2", "title": "근거", "text": "근거."}]},
                         {"docs": [{"id": "DOC-3", "title": "제목만"}]}]}
    assert ragas.dry_scores(case, context, "근거. 잘못된 주장.") == (0.5, 0.5)
    assert ragas.dry_scores(case, {"cards": []}, "") == (0.0, 0.0)


def test_dry_latency_exercises_http_request_and_split_sse_frames():
    report = asyncio.run(latency.dry_run(2))
    assert report["metrics"] == {
        "ttfe": 0.12, "fast_path_ttft": 0.35, "deep_warm_duration": 8.0, "deep_cold_duration": 18.0,
    }
    assert {key: len(value) for key, value in report["samples"].items()} == {"fast": 2, "warm": 2, "cold": 1}
    assert report["passed"] is True
    assert "| Fast Path TTFT | 0.350 | ≤ 1.0 |" in latency.markdown(report)


@pytest.mark.parametrize("frames,message", [
    ("event: error\ndata: {}\n\n", "Backend sent an SSE error"),
    ("event: done\ndata: {\"ok\":false}\n\n", "Expected successful fast_lookup"),
    ("event: token\ndata: {\"text\":\"partial\"}\n\n", "without a complete report"),
    ("event: status\ndata: []\n\n", "Expected an SSE JSON object"),
])
def test_latency_rejects_failed_or_incomplete_streams(frames, message):
    def handle(_):
        return httpx.Response(200, headers={"Content-Type": "text/event-stream"}, content=frames)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            return await latency.measure(client, "http://fixture" + latency.PATH, "", "fast_lookup")

    with pytest.raises(latency.BenchmarkError, match=message):
        asyncio.run(run())


def test_live_latency_measures_one_verified_cold_run_before_warm_runs(monkeypatch):
    codes = {"00000001"}
    snapshots = iter([set(), codes])
    monkeypatch.setattr(latency, "cached_issuers", lambda *_: next(snapshots))
    intents = []
    report = "완료"

    def handle(request):
        query = json.loads(request.content)["user_query"]
        intent = "fast_lookup" if query == latency.FAST_QUERY else "deep_diagnosis"
        intents.append(intent)
        done = {"ok": True, "intent_type": intent, "final_report": report,
                "holdings": [{"asset_type": "STOCK", "corp_code": "00000001", "weight": "0.4",
                              "stock_code": "000001"}],
                "verified_stock_results": [{"stock_code": "000001", "analysis_text": "분석"}]}
        content = (latency.sse("status", {"node": "router_node", "phase": "started"})
                   + latency.sse("token", {"text": report}) + latency.sse("done", done))
        return httpx.Response(200, headers={"Content-Type": "text/event-stream"}, content=content)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            return await latency.live(client, "http://fixture" + latency.PATH, "fixture", codes, 2)

    result = asyncio.run(run())
    assert intents == ["fast_lookup"] * 2 + ["deep_diagnosis"] * 3
    assert result["mode"] == "live"
    assert len(result["samples"]["cold"]) == 1
