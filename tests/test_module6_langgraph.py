import asyncio
from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal as D
import json
from time import perf_counter
from unittest.mock import AsyncMock, Mock, create_autospec

import httpx
from langchain_core.messages import AIMessage
import pytest

from backend import dart_rag, llm, portfolio_market, stock_analysis
from backend.broker import (
    KST, LIVE, VIRTUAL, Balance, BrokerConnector, BrokerSettings, Credentials, DailyBar,
    FiveDayFlow, Holding, InvestorFlow,
)
from backend.connectors import ExternalConnectors
from backend.dart_etl import DartETL
from backend.main import app, chat_events
from backend.portfolio_graph import Intent, build_graph, route_to_stock_workers
from backend.portfolio_market import MarketData
from backend.sync_stock_master import Stock


NOW = datetime(2026, 11, 20, 16, tzinfo=KST)
DAY = NOW.date()
RECEIPT = "20261120000001"


def gemini_result(answer):
    return {"parsed": answer, "raw": AIMessage(content=""), "parsing_error": None}


def position(code="000001", *, asset="STOCK", weight="0.2"):
    return Holding(code, "종목" + code, asset, code.zfill(8) if asset == "STOCK" else None,
                   "KR" + code.zfill(10), D(4), D(120), D(100), D(480), D(400), D(-80),
                   D("-16.67"), D(weight))


def setup_market(monkeypatch, positions):
    broker = create_autospec(BrokerConnector, instance=True)
    broker.clock = lambda: NOW
    broker.http = object()
    broker.get_balance.return_value = Balance(D("1234.56"), D("2000"), D("0.61728"), tuple(positions))
    days = tuple(InvestorFlow(DAY - timedelta(days=i), D(1), D(2)) for i in reversed(range(5)))
    broker.get_five_day_flow.return_value = FiveDayFlow(days, D(5), D(10))
    broker.get_daily_bars.return_value = tuple(
        DailyBar(DAY - timedelta(days=i), *[D(99 - i)] * 4, D(10)) for i in reversed(range(21)))
    etl = create_autospec(DartETL, instance=True)
    external = create_autospec(ExternalConnectors, instance=True)
    external.get_macro_context.return_value = {
        "base_rate": {"date": DAY.isoformat(), "value": D("3.25"), "unit": "%"}}
    external.get_lockup.return_value = {"status": "ok", "releases": []}
    external.get_news.return_value = [{"title": "종목 뉴스", "description": "배당 관련 보도"}]
    stocks = {h.stock_code: Stock(h.stock_code, h.corp_code, h.isin_cd, None, h.name, h.asset_type,
                                  "261", D(10000)) for h in positions}
    monkeypatch.setattr(portfolio_market, "stock_master", AsyncMock(return_value=stocks))
    event = {"corp_code": "00000001", "rcept_no": RECEIPT, "event_type": "CB",
             "board_resolution_date": DAY, "convertible_shares": 10}
    monkeypatch.setattr(portfolio_market, "outstanding_events", AsyncMock(return_value=[event]))
    statement = {"account_id": "dart_OperatingIncomeLoss", "sj_div": "IS",
                 "thstrm_amount": "10", "thstrm_add_amount": "60"}
    reports = {(DAY.year - 1, period, "CFS"): {"status": "000", "list": [row]} for period, row in
               (("11011", statement | {"thstrm_amount": "100"}), ("11014", statement))}
    monkeypatch.setattr(portfolio_market, "financial_reports", AsyncMock(return_value=reports))
    monkeypatch.setattr(dart_rag, "search", AsyncMock(return_value=[
        {"rcept_no": RECEIPT, "report_nm": "발행결정", "chunk_text": "희석률은 3.5%다."}]))
    answer = {"analysis_text": "2026.11.21 기준 [DOC-1] 희석률은 3.5%입니다. "
                               "[DOC-99] 발행은 확정됐습니다. 수급은 안정적입니다.",
              "citations": ["DOC-1", "DOC-99"], "doc_map": {"DOC-99": RECEIPT}}
    async def classify(messages):
        question = messages[-1][1]
        if "추매" in question:
            return Intent(intent_type="simulation")
        if "잔고" in question:
            return Intent(intent_type="fast_lookup")
        return Intent(intent_type="deep_diagnosis")

    monkeypatch.setenv("DASHSCOPE_API_KEY", "dash-test")
    monkeypatch.setenv("GEMINI_API_KEY", "gemini-test")
    router = AsyncMock(side_effect=classify)
    worker = AsyncMock(return_value=stock_analysis.Analysis(**answer))
    summary = AsyncMock(return_value=gemini_result(stock_analysis.Analysis(analysis_text="종합 분석입니다.", citations=[])))
    dash = Mock()
    dash.return_value.with_structured_output.side_effect = lambda schema, **_: Mock(
        ainvoke=router if schema is Intent else worker)
    google = Mock()
    google.return_value.with_structured_output.return_value.ainvoke = summary
    monkeypatch.setattr(llm, "ChatOpenAI", dash)
    monkeypatch.setattr(llm, "ChatGoogleGenerativeAI", google)
    return MarketData(broker, etl, external)


def parse_frame(frame):
    event, data = frame.strip().splitlines()
    return event.removeprefix("event: "), json.loads(data.removeprefix("data: "))


@pytest.mark.parametrize("role,model", [("router", "qwen3.7-plus"), ("worker", "qwen3.8-flash")])
@pytest.mark.parametrize("status", [403, 429, 400])
def test_dashscope_quota_falls_back_once_to_gemini(monkeypatch, role, model, status):
    from openai import APIStatusError

    monkeypatch.setenv("DASHSCOPE_API_KEY", "dash-key")
    monkeypatch.setenv("GEMINI_API_KEY", "gemini-key")
    for key in ("ROUTER_MODEL", "WORKER_MODEL", "SYNTHESIZER_MODEL", "DASHSCOPE_BASE_URL"):
        monkeypatch.delenv(key, raising=False)
    body = {"code": "AllocationQuota.FreeTierOnly"} if status == 400 else {}
    error = APIStatusError(str(body), response=httpx.Response(status, request=httpx.Request("POST", "https://test")),
                           body=body)
    dash, google = Mock(), Mock()
    primary = AsyncMock(side_effect=error)
    fallback = AsyncMock(return_value=gemini_result(Intent(intent_type="deep_diagnosis")))
    dash.return_value.with_structured_output.return_value.ainvoke = primary
    google.return_value.with_structured_output.return_value.ainvoke = fallback
    monkeypatch.setattr(llm, "ChatOpenAI", dash)
    monkeypatch.setattr(llm, "ChatGoogleGenerativeAI", google)
    result = asyncio.run(llm.invoke(role, "분류", "공시", Intent))
    assert result.intent_type == "deep_diagnosis"
    assert dash.call_args.kwargs == dict(api_key="dash-key", model=model, base_url=llm.BASE_URL,
                                         temperature=0, timeout=45 if role == "worker" else 15, max_retries=0,
                                         extra_body={"thinking_budget": llm.QWEN_THINKING_BUDGET})
    assert google.call_args.kwargs["model"] == "gemini-3.8-flash"
    assert google.call_args.kwargs["google_api_key"] == "gemini-key"
    primary.assert_awaited_once()
    fallback.assert_awaited_once_with([("system", "분류"), ("human", "공시")])
    assert primary.await_args.args[0][1] == ("human", "공시")


@pytest.mark.parametrize("positions", [[], [position(asset="ETF")]])
def test_account_snapshot_uses_only_balance_and_orderable_cash(monkeypatch, positions):
    market = setup_market(monkeypatch, positions)
    market.broker.get_orderable_cash.return_value = D("900.12")
    monkeypatch.setattr(app.state, "market", market, raising=False)

    async def get_snapshot():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://test") as client:
            return await client.get("/api/v1/account")

    response = asyncio.run(get_snapshot())
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    snapshot = response.json()
    assert snapshot["account_summary"] == {
        "total_value": "2000", "cash_deposit": "1234.56", "cash_weight": "0.61728",
        "orderable_cash": "900.12",
    }
    if positions:
        assert snapshot["holdings"][0]["asset_type"] == "ETF"
    else:
        assert snapshot["holdings"] == []
    market.broker.get_orderable_cash.assert_awaited_once_with(None)
    market.broker.get_balance.assert_awaited_once_with()
    assert not market.etl.method_calls and not market.external.method_calls


@pytest.mark.parametrize("mode", ["live", "virtual"])
def test_account_snapshot_reports_missing_credentials_before_network_calls(monkeypatch, mode):
    configured = Credentials("fixture-key", "fixture-secret", "12345678")
    incomplete = Credentials("fixture-key", "", "12345678")
    virtual = mode == "virtual"
    settings = BrokerSettings(configured if virtual else incomplete,
                              incomplete if virtual else configured, virtual)
    broker = BrokerConnector(settings, None, None)
    request_broker = AsyncMock()
    monkeypatch.setattr(broker, "_get", request_broker)
    market = MarketData(broker, None, None)
    monkeypatch.setattr(app.state, "market", market, raising=False)

    async def get_snapshot():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://test") as client:
            return await client.get("/api/v1/account")

    response = asyncio.run(get_snapshot())
    assert response.status_code == 503
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {"code": "kis_account_not_configured", "mode": mode}
    request_broker.assert_not_called()


def test_fast_path_stream_and_latency(monkeypatch):
    market = setup_market(monkeypatch, [position()])
    graph = build_graph(market)
    monkeypatch.setattr(app.state, "graph", graph, raising=False)

    async def check():
        # Timestamp actual ASGI body writes, before HTTPX's test transport buffers them.
        frames = []

        async def timed_app(scope, receive, send):
            async def record(message):
                if body := message.get("body"):
                    frames.append((perf_counter() - start, *parse_frame(body.decode())))
                await send(message)
            await app(scope, receive, record)

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=timed_app),
                                     base_url="http://test") as client:
            start = perf_counter()
            response = await client.post("/api/v1/chat/stream", json={"user_query": "내 잔고랑 현재가 얼마야?"})
        assert response.status_code == 200 and response.headers["content-type"].startswith("text/event-stream")
        assert frames[0][1:] == ("status", {"node": "router_node", "phase": "started"})
        assert frames[0][0] <= 0.5
        tokens = [(t, data["text"]) for t, event, data in frames if event == "token"]
        assert tokens[0][0] <= 1.0
        nodes = [d["node"] for _, ev, d in frames if ev == "status" and d["phase"] == "started"]
        assert nodes == ["router_node", "fast_quote_node", "synthesizer_node"]
        _, event, done = frames[-1]
        assert event == "done" and done["ok"] and done["intent_type"] == "fast_lookup"
        assert done["account_summary"]["cash_deposit"] == "1234.56"
        assert done["holdings"][0]["current_price"] == "100"
        assert done["final_report"] == "".join(text for _, text in tokens)
        assert "stock_analysis_results" not in done

    asyncio.run(check())
    market.broker.get_balance.assert_awaited_once_with()
    assert not market.etl.method_calls and not market.external.method_calls
    assert not market.broker.get_five_day_flow.called and not dart_rag.search.called
    portfolio_market.stock_master.assert_not_called()
    llm.ChatOpenAI.assert_called_once()
    llm.ChatGoogleGenerativeAI.assert_not_called()


@pytest.mark.parametrize("positions", [[], [position(asset="ETF", weight="1")],
                                      [position(asset="ETN", weight="1")]])
def test_empty_fanout_reaches_synthesis(monkeypatch, positions):
    market = setup_market(monkeypatch, positions)
    cash = D(0) if positions else D(400)
    market.broker.get_balance.return_value = Balance(cash, D(400), cash / D(400), tuple(positions))
    graph = build_graph(market)
    result = asyncio.run(graph.ainvoke({"user_query": "내 계좌의 리스크를 진단해 줘"}))
    assert route_to_stock_workers(result) == "synthesizer_node"
    assert result["intent_type"] == "deep_diagnosis"
    assert result["account_summary"]["total_value"] == D(400)
    assert stock_analysis.EVIDENCE_HEADING in result["final_report"]
    assert result["verified_stock_results"] == [] and result["stock_analysis_results"] == []
    if positions:
        assert positions[0].name in result["final_report"]
        assert "five_day_flow" in result["quant_scorecards"]["000001"]
        assert "scores" not in result["quant_scorecards"]["000001"]
    else:
        assert "보유 종목이 없어" in result["final_report"]
    market.etl.ingest_stock.assert_not_called()
    market.external.get_lockup.assert_not_called()
    dart_rag.search.assert_not_called()


def test_top_three_worker_isolation_guardrail_and_repeated_requests(monkeypatch):
    positions = [position(f"{n:06}", weight=f"0.{n}") for n in range(1, 5)]
    market = setup_market(monkeypatch, [*positions, position("000005", asset="ETF", weight="0.5")])
    graph = build_graph(market)

    synthesis = llm.ChatGoogleGenerativeAI.return_value.with_structured_output.return_value.ainvoke
    synthesis.return_value = gemini_result(stock_analysis.Analysis(
        analysis_text=f"{stock_analysis.ANSWER_HEADING}\n[DOC-1]과 [DOC-3]에 근거한 진단입니다.\n"
                      f"{stock_analysis.EVIDENCE_HEADING}\n- 전환 가능 물량",
        citations=["DOC-1", "DOC-3"]))
    async def search(corp_code, query):
        return [{"rcept_no": RECEIPT[:-1] + corp_code[-1], "report_nm": "공시",
                 "chunk_text": "2026.11.21 희석률은 3.5%입니다."}]

    dart_rag.search.side_effect = search

    async def check():
        state = {"user_query": "포트폴리오 위험 진단"}
        for _ in range(2):
            # Even accidental replay of an old graph output cannot re-seed the reducer.
            state = await graph.ainvoke(state)
            raw, checked = state["stock_analysis_results"], state["verified_stock_results"]
            assert [r["stock_code"] for r in raw] == ["000004", "000003", "000002"]
            assert len(checked) == 3 and len(state["guardrail_warnings"]) == 3
            for original, row in zip(raw, checked, strict=True):
                receipt = RECEIPT[:-1] + row["stock_code"][-1]
                assert original["doc_map"]["DOC-1"] == receipt and "DOC-99" in original["analysis_text"]
                assert row["analysis_text"] == (f"2026.11.21 기준 [{receipt}] 희석률은 3.5%입니다. "
                                                "수급은 안정적입니다.")
                assert row["citations"] == [receipt]
            summary, evidence = state["final_report"].split(stock_analysis.EVIDENCE_HEADING)
            assert "[DOC-1]과 [DOC-3]에 근거한 진단입니다." in summary
            assert f"rcpNo={RECEIPT[:-1]}2" in evidence and f"rcpNo={RECEIPT[:-1]}4" in evidence
            assert "발행은 확정" not in state["final_report"]
            assert all("DOC-99" in w for w in state["guardrail_warnings"])
            assert state["quant_scorecards"]["000001"]["total"] == 90
    asyncio.run(check())


def test_guardrail_rejects_malformed_worker_independently():
    invalid = {"stock_code": "bad", "analysis_text": [], "citations": "DOC-1", "doc_map": {}}
    valid = {"stock_code": "ok", "analysis_text": "[DOC-1] 자료입니다.",
             "citations": ["DOC-1"], "doc_map": {"DOC-1": RECEIPT}}
    result = stock_analysis.verify_results([invalid, valid])
    assert [row["stock_code"] for row in result["verified_stock_results"]] == ["ok"]
    assert result["verified_stock_results"][0]["citations"] == [RECEIPT]
    assert len(result["guardrail_warnings"]) == 1


def test_guardrail_citations_at_sentence_end():
    worker = {"stock_code": "ok", "analysis_text": "희석률은 3.5%다 [DOC-1]. 가짜다 [DOC-99].",
              "citations": ["DOC-1", "DOC-99"], "doc_map": {"DOC-1": RECEIPT}}
    result = stock_analysis.verify_results([worker])
    assert result["verified_stock_results"][0]["analysis_text"] == f"희석률은 3.5%다 [{RECEIPT}]."
    assert len(result["guardrail_warnings"]) == 1


@pytest.mark.parametrize("virtual", [True, False])
def test_orderable_cash_uses_account_credentials_and_decimal(virtual):
    settings = BrokerSettings(Credentials("real", "secret", "111"),
                              Credentials("paper", "secret", "222"), virtual)
    broker = BrokerConnector(settings, None, None)
    broker._get = AsyncMock(return_value=({"output": {"ord_psbl_cash": "1,234.56"}}, {}))
    cash = asyncio.run(MarketData(broker, None, None).orderable_cash(position()))
    assert cash == D("1234.56")
    domain, credentials, path, tr_id, params = broker._get.await_args.args
    assert (domain, credentials, tr_id) == (
        (VIRTUAL, broker.settings.virtual, "VTTC8908R") if virtual
        else (LIVE, broker.settings.live, "TTTC8908R"))
    assert path == "/trading/inquire-psbl-order" and params["CANO"] == credentials.account_no


def test_simulation_uses_orderable_cash_and_module3(monkeypatch):
    h = position(asset="ETF")
    market = setup_market(monkeypatch, [h])
    market.orderable_cash = AsyncMock(return_value=D(500))
    graph = build_graph(market)
    state = asyncio.run(graph.ainvoke({"user_query": f"{h.name} 주문 가능 현금의 40%로 추매하면?"}))
    assert state["intent_type"] == "simulation"
    market.orderable_cash.assert_awaited_once_with(h)
    result = state["simulation_result"]
    assert result["buy_quantity"] == D(2) and result["remaining_cash"] == D(300)
    assert result["weight"] == D("0.3") and "예상 비중은 30.00%입니다." in state["final_report"]


def test_provider_error_terminates_stream(monkeypatch):
    market = setup_market(monkeypatch, [])
    market.broker.get_balance.side_effect = RuntimeError("private-provider-detail")

    async def read():
        return [parse_frame(frame) async for frame in chat_events(build_graph(market), "잔고 조회")]
    frames = asyncio.run(read())
    assert [ev for ev, _ in frames[-2:]] == ["error", "done"]
    assert frames[-1][1] == {"ok": False} and "private-provider-detail" not in str(frames)


def test_health_and_request_boundary():
    async def check():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            assert (await client.get("/health")).json() == {"status": "ok"}
            for payload in ({"user_query": "  "}, {"user_query": "잔고", "stock_analysis_results": [{}]}):
                response = await client.post("/api/v1/chat/stream", json=payload)
                assert response.status_code == 422
    asyncio.run(check())

def test_cloud_prompts_allow_only_masked_indicators_and_disclosures(monkeypatch):
    stock = replace(position(), quantity=D("812345"), market_value=D("987654321"))
    market = setup_market(monkeypatch, [stock])
    market.broker.get_balance.return_value = Balance(D("987123456"), D("1974777777"), D("0.50"), (stock,))
    query = "계좌 87654321-01의 812345주, 평가액 987654321원, 예수금 987123456원 위험 진단"
    captured = {}
    structured = llm.ChatOpenAI.return_value.with_structured_output
    original = structured.side_effect

    def record(schema, **kwargs):
        runnable = original(schema, **kwargs)
        captured[schema] = runnable.ainvoke
        return runnable

    structured.side_effect = record
    state = asyncio.run(build_graph(market).ainvoke({"user_query": query}))
    assert stock_analysis.ANSWER_HEADING in state["final_report"]
    worker = json.loads(captured[stock_analysis.Analysis].await_args.args[0][-1][1])
    synthesis = llm.ChatGoogleGenerativeAI.return_value.with_structured_output.return_value.ainvoke
    portfolio = json.loads(synthesis.await_args.args[0][-1][1])
    assert portfolio == [worker]
    assert worker == {"name": stock.name, "weight_pct": "20.0", "profit_loss_pct": "-16.67", "scorecard": 90,
                      "disclosures": [{"source": "DOC-1", "title": "발행결정", "text": "희석률은 3.5%다."}]}
    for secret in ("87654321", "812345", "987654321", "987123456", "1974777777", RECEIPT):
        for invocation in (*captured.values(), synthesis):
            assert secret not in str(invocation.await_args)


@pytest.mark.parametrize("missing_keys", [False, True])
def test_total_llm_outage_streams_scores_simulation_and_disclosure_evidence(monkeypatch, missing_keys):
    stock = position()
    market = setup_market(monkeypatch, [stock])
    market.orderable_cash = AsyncMock(return_value=D(500))
    if missing_keys:
        monkeypatch.delenv("DASHSCOPE_API_KEY")
        monkeypatch.delenv("GEMINI_API_KEY")
    else:
        failure = httpx.HTTPStatusError("private-provider-detail", request=httpx.Request("POST", "https://test"),
                                        response=httpx.Response(429))
        llm.ChatOpenAI.return_value.with_structured_output.side_effect = None
        llm.ChatOpenAI.return_value.with_structured_output.return_value.ainvoke = AsyncMock(side_effect=failure)
        llm.ChatGoogleGenerativeAI.return_value.with_structured_output.return_value.ainvoke = AsyncMock(
            side_effect=TimeoutError("private-provider-detail"))

    async def read():
        return [parse_frame(f) async for f in chat_events(build_graph(market), f"{stock.name} 200원 추매하면?")]

    frames = asyncio.run(read())
    events = [event for event, _ in frames]
    assert events[0] == "status" and events[-1] == "done" and "error" not in events
    done = frames[-1][1]
    assert done["ok"] and done["quant_scorecards"][stock.stock_code]["total"] == 90
    assert done["quant_scorecards"][stock.stock_code]["dilution_pct"] == "0.1"
    assert done["simulation_result"]["buy_quantity"] == "2"
    assert done["verified_stock_results"][0]["evidence"] == {"DOC-1": RECEIPT}
    report = "".join(data["text"] for kind, data in frames if kind == "token")
    assert report == done["final_report"] and "90/100" in report and "DOC-1" in report and RECEIPT in report
    warnings = done["guardrail_warnings"]
    assert len(warnings) == 3
    reason = "API키 미설정" if missing_keys else "응답 시간초과"
    assert any("종합 LLM" in warning and f"[{reason}]" in warning for warning in warnings)
    assert "private-provider-detail" not in str(frames)
    if missing_keys:
        llm.ChatOpenAI.assert_not_called()
        llm.ChatGoogleGenerativeAI.assert_not_called()
    else:
        assert llm.ChatGoogleGenerativeAI.call_count == 3


def test_real_langchain_adapters_parse_gemini_fallback(monkeypatch):
    from unittest.mock import PropertyMock

    from google.genai.models import AsyncModels
    from google.genai.types import GenerateContentResponse
    from openai import RateLimitError
    from openai.resources.chat.completions import AsyncCompletions

    monkeypatch.setenv("DASHSCOPE_API_KEY", "offline-dash")
    monkeypatch.setenv("GEMINI_API_KEY", "offline-gemini")
    response = httpx.Response(429, request=httpx.Request("POST", "https://test"))
    dash = AsyncMock(side_effect=RateLimitError("rate limit", response=response, body={}))
    google = AsyncMock(return_value=GenerateContentResponse.model_validate({"candidates": [{
        "content": {"role": "model", "parts": [{"text": '{"analysis_text":"공시 분석","citations":[]}'}]},
        "finishReason": "STOP"}]}))
    monkeypatch.setattr(AsyncCompletions, "with_raw_response", PropertyMock(return_value=Mock(parse=dash)))
    monkeypatch.setattr(AsyncModels, "generate_content", google)
    assert asyncio.run(stock_analysis.analyze('{"name":"가상 종목"}')) == {"analysis_text": "공시 분석", "citations": []}
    dash.assert_awaited_once()
    google.assert_awaited_once()
