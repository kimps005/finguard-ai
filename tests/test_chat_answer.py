"""Chat answers: target cards, the answer-model context, tag renumbering and sentence verification."""

import asyncio
from dataclasses import asdict, replace
from decimal import Decimal as D
import json
from unittest.mock import AsyncMock

import httpx
from openai import APIStatusError
import pytest

from backend import llm, price_card, stock_analysis
from backend.broker import BrokerSettings, Credentials
from backend.main import app, chat_events
from backend.portfolio_graph import build_graph, route_to_stock_workers, target_codes
from backend.report import answer_report
from test_module6_langgraph import RECEIPT, card_fixture, parse_frame, position, setup_market


ANSWER, EVIDENCE = stock_analysis.ANSWER_HEADING, stock_analysis.EVIDENCE_HEADING


def reply(core, citations=()):
    return stock_analysis.Analysis(analysis_text=f"{ANSWER}\n\n{core}\n\n{EVIDENCE}\n\n- 카드 근거",
                                   citations=list(citations))


def capture_answer(monkeypatch, answer):
    calls = []

    async def invoke(role, instruction, context, schema, **kwargs):
        calls.append((role, instruction, json.loads(context)))
        return answer

    monkeypatch.setattr(llm, "invoke", invoke)
    return calls


def stream(market, query, selected=None):
    async def read():
        return [parse_frame(frame) async for frame in chat_events(build_graph(market), query, selected)]
    return asyncio.run(read())


def answer_state(rows, query="추가매수할까? 홀드할까?", holdings=()):
    return {"user_query": query, "holdings": [asdict(h) for h in holdings],
            "account_summary": {"total_value": D(2000), "cash_deposit": D("1234.56"), "cash_weight": D("0.61728")},
            "verified_stock_results": [{"stock_code": band["stock_code"], "card": band} for band in rows],
            "simulation_result": None}


def test_selected_non_held_stock_gets_a_card_and_answer(monkeypatch):
    market = setup_market(monkeypatch, [position("000001")])
    monkeypatch.setattr(app.state, "graph", build_graph(market), raising=False)
    calls = capture_answer(monkeypatch, reply(
        "선택 종목은 [P-2] 20일선 95원을 지키면 추가매수를 검토할 수 있고, 밑돌면 관망이 유리한 조건입니다.",
        ["P-2"]))

    async def post(payload):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            return await client.post("/api/v1/chat/stream", json=payload)

    response = asyncio.run(post({"user_query": "추가매수할까? 홀드할까?", "selected_stock_code": "005930"}))
    frames = [parse_frame(frame) for frame in response.text.split("\n\n") if frame.strip()]
    event, done = frames[-1]
    assert event == "done" and done["ok"]
    # No holding is named: the selected (not held, no score inputs) stock first, then the top holding.
    assert [call.args[1] for call in price_card.get_or_build_card.await_args_list] == ["005930", "000001"]
    workers = [data for kind, data in frames
               if kind == "status" and data["node"] == "stock_worker" and data["phase"] == "started"]
    assert sorted(data["stock_code"] for data in workers) == ["000001", "005930"]
    role, _, context = calls[0]
    assert role == "answer" and [card["stock_code"] for card in context["cards"]] == ["005930", "000001"]
    report = done["final_report"]
    assert report.startswith(f"{ANSWER}\n\n선택 종목은 [P-2] 20일선 95원을 지키면")
    assert "[P-2] 종목005930 20일선 95원" in report.split(EVIDENCE)[1]
    assert [row["stock_code"] for row in done["verified_stock_results"]] == ["005930", "000001"]


def test_targets_union_selected_and_mentioned_capped_at_three():
    def row(code, weight, asset="STOCK"):
        return {"stock_code": code, "name": "종목" + code, "asset_type": asset, "weight": D(weight)}

    holdings = [row("000001", "0.1"), row("000002", "0.4"), row("000003", "0.2"), row("000004", "0.3"),
                row("000005", "0.5", "ETF")]
    state = {"user_query": "종목000004와 000002, 종목000003 중 뭘 더 살까?", "holdings": holdings,
             "selected_stock_code": "999999"}
    # Selected first, then holdings in the order the question names them; no score_inputs needed.
    assert target_codes(state) == ["999999", "000004", "000002"]
    assert target_codes({**state, "selected_stock_code": "000002"}) == ["000002", "000004", "000003"]
    assert target_codes({**state, "user_query": "종목000005 어때?", "selected_stock_code": None}) == ["000005"]
    # Nothing selected or named: the three heaviest STOCK holdings.
    assert target_codes({**state, "user_query": "어때?", "selected_stock_code": None}) == [
        "000002", "000004", "000003"]
    # A portfolio question names no holding: the selected card, then the heaviest STOCK holdings.
    portfolio = {**state, "user_query": "포트폴리오 위험 진단"}
    assert target_codes(portfolio) == ["999999", "000002", "000004"]
    assert target_codes({**portfolio, "selected_stock_code": "000004"}) == ["000004", "000002", "000003"]
    sends = route_to_stock_workers(state)
    assert [(send.node, send.arg) for send in sends] == [
        ("stock_worker", {"stock_code": "999999", "name": None}),
        ("stock_worker", {"stock_code": "000004", "name": "종목000004"}),
        ("stock_worker", {"stock_code": "000002", "name": "종목000002"})]
    assert route_to_stock_workers({"user_query": "어때?", "holdings": [row("000005", "1", "ETF")]}) == "synthesizer_node"


def test_answer_context_has_question_and_account_but_no_account_number(monkeypatch):
    stock = replace(position(), quantity=D("812345"))
    market = setup_market(monkeypatch, [stock])
    market.broker.settings = BrokerSettings(Credentials("kis-live-key", "kis-live-secret", "87654321"),
                                            Credentials("kis-paper-key", "kis-paper-secret", "12349876"), False)
    monkeypatch.setenv("APP_ACCESS_PIN", "pin-2468")
    monkeypatch.setenv("DART_API_KEY", "dart-key-1")
    calls = capture_answer(monkeypatch, reply("[P-2] 20일선 95원 위라면 보유를 검토할 조건입니다.", ["P-2"]))
    query = f"{stock.name} 추가매수할까? 홀드할까?"
    event, done = stream(market, query)[-1]
    assert event == "done" and done["ok"]
    role, _, context = calls[0]
    assert set(context) == {"question", "account", "cards", "simulation"}
    assert context["question"] == query
    assert context["account"] == {"holdings": [{
        "name": stock.name, "code": stock.stock_code, "quantity": "812345", "average_price": "120",
        "current_price": "100", "weight_pct": "20.00", "profit_loss_pct": "-16.67"}],
        "cash": "1234.56", "total": "2000"}
    assert context["simulation"] == {"message": "사용할 주문 가능 현금의 비율(%) 또는 투자 금액(원)을 한 개 지정해 주세요."}
    raw = json.dumps(context, ensure_ascii=False)
    for secret in ("87654321", "12349876", "kis-live", "kis-paper", "pin-2468", "dart-key-1",
                   "dash-test", "gemini-test", RECEIPT):
        assert secret not in raw


def test_tags_from_two_cards_are_renumbered_without_collision(monkeypatch):
    first = card_fixture("000001", "알파", docs=(("알파 공시", "20261120000001"),),
                         news=[("알파 수주", "https://news.example/a")])["band"]
    second = card_fixture("000002", "베타", docs=(("베타 공시", "20261120000002"),),
                          news=[("베타 증설", "https://news.example/b"), ("베타 루머", "javascript:alert(1)")])["band"]
    calls = capture_answer(monkeypatch, reply(
        "베타는 [DOC-2] 공시와 [NEWS-2] 보도로 보면 [P-5] 20일선 95원을 지킬 때 매수를 검토할 조건입니다. "
        "[NEWS-3] 소문은 확인되지 않았습니다.", ["DOC-2", "NEWS-2", "NEWS-3"]))
    state = answer_state([first, second], query="알파랑 베타 중 뭘 살까?")
    core, sources, warnings = asyncio.run(stock_analysis.answer(state))
    cards = calls[0][2]["cards"]
    assert [[n["id"] for n in card["news"]] for card in cards] == [["NEWS-1"], ["NEWS-2", "NEWS-3"]]
    assert [[d["id"] for d in card["docs"]] for card in cards] == [["DOC-1"], ["DOC-2"]]
    assert [[f["id"] for f in card["facts"]] for card in cards] == [["P-1", "P-2", "P-3"], ["P-4", "P-5", "P-6"]]
    assert cards[1]["levels"]["buy_zone"]["reason"] == "[P-5] 20일선 지지 [DOC-2]"
    assert cards[1]["levels"]["buy_zone"]["basis"] == ["P-5"]
    assert cards[1]["risks"] == [{"text": "실적 발표 일정 [DOC-2]", "basis": ["DOC-2"]}]
    assert cards[1]["summary"] == "[P-5] 20일선 위에서 거래됩니다."
    assert warnings == []
    evidence = answer_report(state, core, sources).split(EVIDENCE)[1]
    # Each cited tag links the second card's own source; uncited sources are not listed.
    assert "[DOC-2](https://dart.fss.or.kr/dsaf001/main.do?rcpNo=20261120000002) · 베타 공시" in evidence
    assert "[NEWS-2](https://news.example/b) · 베타 증설" in evidence
    assert "[NEWS-3] 베타 루머" in evidence and "javascript:" not in evidence
    assert "20261120000001" not in evidence and "news.example/a" not in evidence


def test_sentence_with_unlisted_price_is_dropped_with_warning(monkeypatch):
    band = card_fixture("000001", "종목000001")["band"]
    capture_answer(monkeypatch, reply(
        "[P-2] 20일선 95원을 지키면 추가매수를 검토할 수 있는 조건입니다. 목표가는 3,900원입니다. "
        "질문하신 3.5% 하락은 16.67% 손실 구간과 겹칩니다. 공시 20261120000009를 참고하세요. "
        "[DOC-7] 공시는 호재입니다.\n\n보유 수량 4주는 그대로 두는 조건입니다.", ["P-2"]))
    state = answer_state([band], query="3.5% 빠졌는데 추가매수할까?", holdings=[position()])
    core, _, warnings = asyncio.run(stock_analysis.answer(state))
    assert core == ("[P-2] 20일선 95원을 지키면 추가매수를 검토할 수 있는 조건입니다. "
                    "질문하신 3.5% 하락은 16.67% 손실 구간과 겹칩니다.\n\n보유 수량 4주는 그대로 두는 조건입니다.")
    assert "종합 진단 입력에 없는 수치 문장 제외 (3,900원)" in warnings
    assert "종합 진단 미등록 인용 제외 (DOC-7)" in warnings
    assert "종합 진단 별칭 없는 공시번호 문장 제외" in warnings
    # An answer with no surviving sentence is unusable; the local report takes over.
    capture_answer(monkeypatch, reply("목표가는 3,900원입니다."))
    with pytest.raises(llm.LLMUnavailable) as failure:
        asyncio.run(stock_analysis.answer(state))
    assert failure.value.reason == "응답 검증 실패"


def test_answer_outage_streams_local_report_and_done_ok(monkeypatch):
    # Default chain, no ANSWER_MODEL override: DashScope 429 -> Gemini once -> local report.
    market = setup_market(monkeypatch, [position()])
    monkeypatch.delenv("ANSWER_MODEL")
    error = APIStatusError("rate limited", response=httpx.Response(429, request=httpx.Request("POST", "https://test")),
                           body={})
    llm.ChatOpenAI.return_value.with_structured_output.return_value.ainvoke = AsyncMock(side_effect=error)
    llm.ChatGoogleGenerativeAI.return_value.with_structured_output.return_value.ainvoke = AsyncMock(
        side_effect=TimeoutError("private-provider-detail"))
    frames = stream(market, "포트폴리오 위험 진단", "000001")
    events = [event for event, _ in frames]
    assert events[-1] == "done" and "error" not in events
    done = frames[-1][1]
    assert done["ok"] is True
    assert done["guardrail_warnings"] == ["답변 모델을 사용할 수 없어 로컬 정량 리포트를 제공합니다. [응답 시간초과]"]
    assert llm.ChatOpenAI.call_count == 1 and llm.ChatOpenAI.call_args.kwargs["model"] == "qwen3.8-flash"
    assert llm.ChatGoogleGenerativeAI.call_count == 1
    assert llm.ChatGoogleGenerativeAI.call_args.kwargs["model"] == "gemini-3.8-flash"
    report = "".join(data["text"] for kind, data in frames if kind == "token")
    assert report == done["final_report"]
    assert [line for line in report.splitlines() if line.startswith("### ")] == [ANSWER, EVIDENCE]
    assert "90/100" in report and f"rcpNo={RECEIPT}" in report
    assert "private-provider-detail" not in str(frames)
    # A selected stock the account does not hold still gets its card levels in the local report.
    done = stream(market, "홀드할까?", "005930")[-1][1]
    assert done["ok"] and "**종목005930**(미보유) 2주 가격 카드 기준 단기 저점은 95원" in done["final_report"]
    assert "- 종목005930 2주 가격 카드 · 현재가 100원, 단기 저점 95원" in done["final_report"]
