import asyncio
from datetime import datetime, timedelta
from decimal import Decimal as D
import json
from time import perf_counter
from unittest.mock import AsyncMock, Mock, create_autospec

import httpx
from langchain_core.messages import AIMessage
import pytest

from backend import dart_rag, llm, portfolio_market, price_card, stock_analysis
from backend.broker import (
    KST, LIVE, VIRTUAL, Balance, BrokerConnector, BrokerError, BrokerSettings, Credentials, DailyBar,
    FiveDayFlow, Holding, InvestorFlow,
)
from backend.connectors import ExternalConnectors
from backend.dart_etl import DartETL
from backend.main import app, chat_events
from backend.portfolio_graph import build_graph, route_to_stock_workers
from backend.portfolio_market import MarketData
from backend.sync_stock_master import Stock


NOW = datetime(2026, 11, 20, 16, tzinfo=KST)
DAY = NOW.date()
RECEIPT = "20261120000001"


def gemini_result(answer):
    return {"parsed": answer, "raw": AIMessage(content=""), "parsing_error": None}


def card_fixture(code, name, *, docs=(("발행결정", RECEIPT),), news=(), warnings=(), current=D(100)):
    """A validated 2-week price card as price_card.get_or_build_card returns it."""
    def level(price, reason, basis):
        return {"price": D(price), "reason": reason, "current_pct": (D(price) - current) * 100 / current,
                "source": "ai", "basis": basis}

    return {"as_of": NOW.isoformat(), "band": {
        "stock_code": code, "name": name, "current_price": current, "change_pct": D("1.00"),
        "levels": {"buy_zone": level(95, "[P-2] 20일선 지지 [DOC-1]" if docs else "[P-2] 20일선 지지", ["P-2"]),
                   "sell_zone": level(105, "[P-1] 현재가 위 저항", ["P-1"]),
                   "peak_limit": level(110, "[P-1] 현재가 위 상단", ["P-1"])},
        "summary": "[P-2] 20일선 위에서 거래됩니다.",
        "risks": [{"text": "실적 발표 일정 [DOC-1]", "basis": ["DOC-1"]}] if docs else [],
        "news": [{"alias": f"NEWS-{i}", "title": title, "link": link} for i, (title, link) in enumerate(news, 1)],
        "facts": [{"id": "P-1", "label": "현재가", "value": current, "upper": None, "session": None},
                  {"id": "P-2", "label": "20일선", "value": D(95), "upper": None, "session": None},
                  {"id": "P-3", "label": "외국인 5일 순매수", "value": D(5), "upper": None, "session": None}],
        "disclosures": [{"alias": f"DOC-{i}", "title": title, "receipt": receipt}
                        for i, (title, receipt) in enumerate(docs, 1)],
        "ai_status": "ai", "warnings": list(warnings), "horizon_sessions": 10}}


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
    monkeypatch.setattr(portfolio_market, "fetch_stocks", AsyncMock(return_value=stocks))
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
    names = {h.stock_code: h.name for h in positions}

    async def card(market, code):
        return card_fixture(code, names.get(code, "종목" + code))

    # Workers fetch cards; the card builder itself is covered by test_price_card.
    monkeypatch.setattr(price_card, "get_or_build_card", AsyncMock(side_effect=card))

    async def updates(market, code):
        # Workers stream card_updates; the graph tests drive it through the get_or_build_card mock.
        yield await price_card.get_or_build_card(market, code)

    monkeypatch.setattr(price_card, "card_updates", updates)
    monkeypatch.setenv("DASHSCOPE_API_KEY", "dash-test")
    monkeypatch.setenv("GEMINI_API_KEY", "gemini-test")
    # Most graph tests assert the answer prompt and reply on the Gemini mock.
    monkeypatch.setenv("ANSWER_MODEL", "gemini-3.8-flash")
    worker = AsyncMock(return_value=stock_analysis.Analysis(**answer))
    summary = AsyncMock(return_value=gemini_result(stock_analysis.Analysis(analysis_text="종합 분석입니다.", citations=[])))
    dash = Mock()
    dash.return_value.with_structured_output.return_value.ainvoke = worker
    google = Mock()
    google.return_value.with_structured_output.return_value.ainvoke = summary
    monkeypatch.setattr(llm, "ChatOpenAI", dash)
    monkeypatch.setattr(llm, "ChatGoogleGenerativeAI", google)
    return MarketData(broker, etl, external)


def parse_frame(frame):
    event, data = frame.strip().splitlines()
    return event.removeprefix("event: "), json.loads(data.removeprefix("data: "))


@pytest.mark.parametrize("role,model", [("analyst", "qwen3.7-plus"), ("answer", "qwen3.8-flash")])
@pytest.mark.parametrize("status", [403, 429, 400])
def test_dashscope_quota_falls_back_once_to_gemini(monkeypatch, role, model, status):
    from openai import APIStatusError

    monkeypatch.setenv("DASHSCOPE_API_KEY", "dash-key")
    monkeypatch.setenv("GEMINI_API_KEY", "gemini-key")
    for key in ("ANALYST_MODEL", "ANSWER_MODEL", "FALLBACK_MODEL", "DASHSCOPE_BASE_URL"):
        monkeypatch.delenv(key, raising=False)
    body = {"code": "AllocationQuota.FreeTierOnly"} if status == 400 else {}
    error = APIStatusError(str(body), response=httpx.Response(status, request=httpx.Request("POST", "https://test")),
                           body=body)
    dash, google = Mock(), Mock()
    primary = AsyncMock(side_effect=error)
    fallback = AsyncMock(return_value=gemini_result(stock_analysis.Analysis(analysis_text="분석", citations=[])))
    dash.return_value.with_structured_output.return_value.ainvoke = primary
    google.return_value.with_structured_output.return_value.ainvoke = fallback
    monkeypatch.setattr(llm, "ChatOpenAI", dash)
    monkeypatch.setattr(llm, "ChatGoogleGenerativeAI", google)
    result = asyncio.run(llm.invoke(role, "분류", "공시", stock_analysis.Analysis))
    assert result.analysis_text == "분석"
    assert dash.call_args.kwargs == dict(api_key="dash-key", model=model, base_url=llm.BASE_URL,
                                         temperature=0, timeout=30, max_retries=0,
                                         extra_body={"thinking_budget": llm.QWEN_THINKING_BUDGET[role]})
    assert google.call_args.kwargs["model"] == "gemini-3.8-flash"
    assert google.call_args.kwargs["google_api_key"] == "gemini-key"
    primary.assert_awaited_once()
    fallback.assert_awaited_once()
    assert fallback.await_args.args == ([("system", "분류"), ("human", "공시")],)
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
    portfolio_market.fetch_stocks.assert_not_called()
    llm.ChatOpenAI.assert_not_called()
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

    async def card(market, code):
        return card_fixture(code, "종목" + code, docs=(("공시", RECEIPT[:-1] + code[-1]),))

    price_card.get_or_build_card.side_effect = card
    synthesis = llm.ChatGoogleGenerativeAI.return_value.with_structured_output.return_value.ainvoke
    # Cards arrive 000004, 000003, 000002: each card's DOC-1 becomes DOC-1, DOC-2, DOC-3 in the answer.
    synthesis.return_value = gemini_result(stock_analysis.Analysis(
        analysis_text=f"{stock_analysis.ANSWER_HEADING}\n[DOC-1]과 [DOC-3]에 근거한 진단입니다.\n"
                      f"{stock_analysis.EVIDENCE_HEADING}\n- 전환 가능 물량",
        citations=["DOC-1", "DOC-3"]))

    async def check():
        state = {"user_query": "포트폴리오 위험 진단"}
        for _ in range(2):
            # Even accidental replay of an old graph output cannot re-seed the reducer.
            state = await graph.ainvoke(state)
            raw, checked = state["stock_analysis_results"], state["verified_stock_results"]
            assert [r["stock_code"] for r in raw] == ["000004", "000003", "000002"]
            assert checked == raw and state["guardrail_warnings"] == []
            for row in raw:
                receipt = RECEIPT[:-1] + row["stock_code"][-1]
                assert row["doc_map"] == {"DOC-1": receipt}
                assert row["disclosures"] == [{"source": "DOC-1", "title": "공시", "text": ""}]
                assert row["card"]["stock_code"] == row["stock_code"] and row["news"] == []
            summary, evidence = state["final_report"].split(stock_analysis.EVIDENCE_HEADING)
            assert "[DOC-1]과 [DOC-3]에 근거한 진단입니다." in summary
            assert f"rcpNo={RECEIPT[:-1]}4" in evidence and f"rcpNo={RECEIPT[:-1]}2" in evidence
            assert f"rcpNo={RECEIPT[:-1]}3" not in evidence
            assert state["quant_scorecards"]["000001"]["total"] == 90
    asyncio.run(check())
    assert price_card.get_or_build_card.await_count == 6
    llm.ChatOpenAI.assert_not_called()


def test_guardrail_rejects_malformed_worker_independently():
    invalid = {"stock_code": "bad", "analysis_text": [], "citations": "DOC-1", "doc_map": {}}
    valid = {"stock_code": "ok", "analysis_text": "[DOC-1] 자료입니다.",
             "citations": ["DOC-1"], "doc_map": {"DOC-1": RECEIPT}}
    result = stock_analysis.verify_results([invalid, valid], allowed=set())
    assert [row["stock_code"] for row in result["verified_stock_results"]] == ["ok"]
    assert result["verified_stock_results"][0]["citations"] == ["DOC-1"]
    assert len(result["guardrail_warnings"]) == 1


def test_guardrail_citations_at_sentence_end():
    worker = {"stock_code": "ok", "analysis_text": "희석률은 3.5%다 [DOC-1]. 가짜다 [NEWS-9]. 근거는 [P-2]다.",
              "citations": ["DOC-1", "NEWS-9"], "doc_map": {"DOC-1": RECEIPT, "P-2": "P-2"}}
    result = stock_analysis.verify_results([worker], allowed={D("3.5")})
    row = result["verified_stock_results"][0]
    assert row["analysis_text"] == "희석률은 3.5%다 [DOC-1]. 근거는 [P-2]다."
    assert row["citations"] == ["DOC-1", "P-2"]
    assert result["guardrail_warnings"] == ["ok 미등록 인용 제외 (NEWS-9)"]


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
            for payload in ({"user_query": "  "}, {"user_query": "잔고", "stock_analysis_results": [{}]},
                            {"user_query": "잔고", "selected_stock_code": "00593"},
                            {"user_query": "잔고", "selected_stock_code": "005930;"}):
                response = await client.post("/api/v1/chat/stream", json=payload)
                assert response.status_code == 422
    asyncio.run(check())

def test_answer_prompt_carries_cards_by_tag_and_never_a_receipt(monkeypatch):
    stock = position()
    market = setup_market(monkeypatch, [stock])
    query = f"{stock.name} 위험 진단"
    state = asyncio.run(build_graph(market).ainvoke({"user_query": query}))
    assert stock_analysis.ANSWER_HEADING in state["final_report"]
    synthesis = llm.ChatGoogleGenerativeAI.return_value.with_structured_output.return_value.ainvoke
    context = json.loads(synthesis.await_args.args[0][-1][1])
    assert set(context) == {"question", "account", "cards", "simulation"} and context["question"] == query
    card = context["cards"][0]
    assert card["docs"] == [{"id": "DOC-1", "title": "발행결정"}]
    assert card["levels"]["buy_zone"]["reason"] == "[P-2] 20일선 지지 [DOC-1]"
    # Receipts stay local: the model cites [DOC-n] and the report links the receipt.
    assert RECEIPT not in str(synthesis.await_args)
    llm.ChatOpenAI.assert_not_called()


def test_deep_diagnosis_default_models_make_one_dashscope_call_and_no_worker_call(monkeypatch):
    market = setup_market(monkeypatch, [position()])
    monkeypatch.delenv("ANSWER_MODEL")
    answer = stock_analysis.Analysis(
        analysis_text="\n".join([stock_analysis.ANSWER_HEADING, "[DOC-1] 기준 진단입니다.",
                                 stock_analysis.EVIDENCE_HEADING, "- 공시"]),
        citations=["DOC-1"])
    llm.ChatOpenAI.return_value.with_structured_output.return_value.ainvoke = AsyncMock(return_value=answer)

    state = asyncio.run(build_graph(market).ainvoke({"user_query": "포트폴리오 위험 진단"}))
    assert "기준 진단입니다." in state["final_report"]
    assert llm.ChatOpenAI.call_count == 1 and llm.ChatOpenAI.call_args.kwargs["model"] == "qwen3.8-flash"
    llm.ChatGoogleGenerativeAI.assert_not_called()


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
    assert done["verified_stock_results"][0]["doc_map"] == {"DOC-1": RECEIPT}
    report = "".join(data["text"] for kind, data in frames if kind == "token")
    assert report == done["final_report"] and "90/100" in report and "DOC-1" in report and RECEIPT in report
    warnings = done["guardrail_warnings"]
    assert len(warnings) == 1
    reason = "API키 미설정" if missing_keys else "응답 시간초과"
    assert warnings == [f"답변 모델을 사용할 수 없어 로컬 정량 리포트를 제공합니다. [{reason}]"]
    assert "private-provider-detail" not in str(frames)
    if missing_keys:
        llm.ChatOpenAI.assert_not_called()
        llm.ChatGoogleGenerativeAI.assert_not_called()
    else:
        assert llm.ChatGoogleGenerativeAI.call_count == 1


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
    answer = asyncio.run(llm.invoke("answer", "분석", '{"name":"가상 종목"}', stock_analysis.Analysis))
    assert answer.model_dump() == {"analysis_text": "공시 분석", "citations": []}
    dash.assert_awaited_once()
    google.assert_awaited_once()


def test_done_event_exposes_every_public_state_field_and_no_internal_one():
    from backend.main import PUBLIC_FIELDS
    from backend.portfolio_graph import PortfolioState

    internal = {"user_query", "selected_stock_code", "stock_analysis_results", "balance", "as_of",
                "score_inputs"}
    assert set(PUBLIC_FIELDS) == set(PortfolioState.__annotations__) - internal


def test_one_failing_source_degrades_only_its_holding(monkeypatch):
    market = setup_market(monkeypatch, [position("000001", weight="0.3"), position("000002", weight="0.4")])
    flow = market.broker.get_five_day_flow.return_value

    async def five_day_flow(code, **kwargs):
        if code == "000002":  # e.g. a new listing without five confirmed sessions
            raise BrokerError("Five confirmed KRX sessions are not available")
        return flow

    market.broker.get_five_day_flow.side_effect = five_day_flow
    market.etl.ingest_stock.side_effect = RuntimeError("DART list.json returned HTTP 500")
    portfolio_market.financial_reports.side_effect = RuntimeError("DART fnlttSinglAcntAll failed")

    async def stream():
        return [parse_frame(frame) async for frame in chat_events(build_graph(market), "포트폴리오 위험 진단")]

    events = asyncio.run(stream())
    event, done = events[-1]
    assert event == "done" and done["ok"] is True
    assert done["quant_scorecards"]["000002"] == {"status": "unavailable"}
    card = done["quant_scorecards"]["000001"]
    assert card["scores"]["flow"] is not None and card["scores"]["earnings"] is None and card["total"] is None
    # A holding without score inputs still gets a card; it keeps its broker mapping.
    assert [row["stock_code"] for row in done["verified_stock_results"]] == ["000002", "000001"]
    assert {row["stock_code"]: row["asset_type"] for row in done["holdings"]} == {"000001": "STOCK", "000002": "STOCK"}
    warnings = done["guardrail_warnings"]
    assert "종목000002 최근 5거래일 확정 수급을 불러오지 못해 정량 점수에서 제외했습니다." in warnings
    assert "종목000001 최신 공시 적재가 끝나지 않아 저장된 공시로 분석했습니다." in warnings
    assert "종목000001 재무제표를 불러오지 못해 실적 점수를 산정하지 않았습니다." in warnings
    assert len(warnings) == len(set(warnings))
    assert "종목000002" in done["final_report"]


def test_card_failure_degrades_only_its_worker(monkeypatch):
    market = setup_market(monkeypatch, [position("000001", weight="0.3"), position("000002", weight="0.4")])

    async def card(market, code):
        if code == "000002":
            raise RuntimeError("private-provider-detail")
        return card_fixture(code, "종목" + code, warnings=["AI 분석을 사용할 수 없어 기본 계산을 사용했습니다."])

    price_card.get_or_build_card.side_effect = card

    async def stream():
        return [parse_frame(frame) async for frame in chat_events(build_graph(market), "포트폴리오 위험 진단")]

    events = asyncio.run(stream())
    event, done = events[-1]
    assert event == "done" and done["ok"] is True
    rows = {row["stock_code"]: row for row in done["verified_stock_results"]}
    assert rows["000002"]["card"] is None and rows["000002"]["doc_map"] == {}
    assert rows["000001"]["card"]["stock_code"] == "000001"
    warnings = done["guardrail_warnings"]
    assert "종목000002 가격 카드를 만들 수 없어 카드 없이 답변합니다." in warnings
    assert "종목000001 가격 카드: AI 분석을 사용할 수 없어 기본 계산을 사용했습니다." in warnings
    assert "private-provider-detail" not in str(events)
    assert done["final_report"]


def test_slow_dart_ingest_is_bounded(monkeypatch):
    market = setup_market(monkeypatch, [position("000001")])
    monkeypatch.setattr(portfolio_market, "INGEST_TIMEOUT", 0.05)
    cancelled = []

    async def stuck(*args):
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            cancelled.append(True)
            raise

    market.etl.ingest_stock.side_effect = stuck
    started = perf_counter()
    result = asyncio.run(build_graph(market).ainvoke({"user_query": "포트폴리오 위험 진단"}))
    assert perf_counter() - started < 10 and cancelled == [True]
    assert any("최신 공시 적재" in warning for warning in result["guardrail_warnings"])
    # Events already in the database still feed the overhang score.
    assert result["quant_scorecards"]["000001"]["dilution_pct"] is not None


def test_worker_status_events_name_their_stock_once(monkeypatch):
    market = setup_market(monkeypatch, [position("000001", weight="0.3"), position("000002", weight="0.4")])

    async def stream():
        return [parse_frame(frame) async for frame in chat_events(build_graph(market), "포트폴리오 위험 진단")]

    workers = [data for event, data in asyncio.run(stream())
               if event == "status" and data["node"] == "stock_worker"]
    started = {d["task_id"]: d for d in workers if d["phase"] == "started"}
    assert sorted((d["stock_code"], d["stock_name"]) for d in started.values()) == [
        ("000001", "종목000001"), ("000002", "종목000002")]
    completed = [d for d in workers if d["phase"] == "completed"]
    assert {d["task_id"] for d in completed} == set(started) and all("stock_code" not in d for d in completed)
    # Other nodes carry no stock fields, and nothing from the user's query leaks into statuses.
    assert all(set(d) == {"node", "phase", "task_id", "stock_code", "stock_name"} for d in started.values())


def test_guardrail_keeps_compound_man_won_figures_that_are_listed():
    worker = {"stock_code": "ok", "analysis_text": "저점은 101만4000원입니다 [P-2]. 목표는 120만3000원입니다 [P-2].",
              "citations": ["P-2"], "doc_map": {"P-2": "P-2"}}
    result = stock_analysis.verify_results([worker], allowed={D(1014000)}, near=[D(1030000)])
    assert result["verified_stock_results"][0]["analysis_text"] == "저점은 101만4000원입니다 [P-2]."
    assert result["guardrail_warnings"] == ["ok 입력에 없는 수치가 있는 문장 1개를 제외했습니다."]
