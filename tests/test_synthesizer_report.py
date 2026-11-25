import asyncio
from dataclasses import replace
from decimal import Decimal
from unittest.mock import AsyncMock

from backend import llm, price_card, stock_analysis
from backend.main import chat_events
from backend.portfolio_graph import build_graph
from test_module6_langgraph import RECEIPT, card_fixture, gemini_result, parse_frame, position, setup_market


def test_answer_and_local_fallback_use_the_same_report_format(monkeypatch):
    holding = replace(position(), average_price=Decimal("3557.727272"),
                      purchase_amount=Decimal("14230.909088"))
    market = setup_market(monkeypatch, [holding])
    market.orderable_cash = AsyncMock(return_value=Decimal(500))
    unused_receipt = "20261120000002"

    async def card(market, code):
        return card_fixture(code, holding.name, docs=(("발행결정", RECEIPT), ("미사용 공시", unused_receipt)))

    price_card.get_or_build_card.side_effect = card
    answer_model = llm.ChatGoogleGenerativeAI.return_value.with_structured_output.return_value.ainvoke
    answer_model.return_value = gemini_result(stock_analysis.Analysis(
        analysis_text="### 핵심 답변\n\n전환 물량이 출회되면 매물 부담이 커질 수 있습니다 [DOC-1]."
                      "\n\n### 판단 근거\n\n- [DOC-1] 전환 물량\n- [DOC-2] 미사용 공시",
        citations=["DOC-1"],
    ))

    async def collect(query):
        return [parse_frame(frame) async for frame in chat_events(
            build_graph(market), f"{holding.name} {query}")]

    for outage in (False, True):
        if outage:
            answer_model.side_effect = TimeoutError
        query = "손절 구간과 200원 추매 시 평단가를 알려 줘" if outage else "손절 구간을 진단해 줘"
        frames = asyncio.run(collect(query))
        event, done = frames[-1]
        assert event == "done" and done["ok"]
        report = "".join(data["text"] for kind, data in frames if kind == "token")
        assert report == done["final_report"]
        assert [line for line in report.splitlines() if line.startswith("### ")] == [
            "### 핵심 답변", "### 판단 근거"]
        assert report.startswith("### 핵심 답변\n\n")
        first_paragraph = report.split("\n\n")[1]
        if outage:
            # The local report answers with the quantitative scenario first.
            assert all(term in first_paragraph for term in ("20일선", "순매수", "오버행", "3,558원", "하방 위험"))
            assert "전환가능 물량" in report and "예상 평단가는 2,405원" in first_paragraph
        else:
            # The answer model's verified text is the answer; only its cited filing is linked.
            assert first_paragraph == "전환 물량이 출회되면 매물 부담이 커질 수 있습니다 [DOC-1]."
            # The evidence repeats the local figures the local report would show, line for line.
            evidence = report.split("### 판단 근거\n\n")[1].splitlines()
            assert evidence[:3] == [f"- {holding.name} · 현재가 100원, 평단가 3,558원, 20일선 90원",
                                    f"- {holding.name} · 오버행 희석률 0.1%",
                                    f"- {holding.name} · 수급 25, 모멘텀 25, 실적 25, 오버행 15점 (합계 90/100)"]
        assert "[DOC-1]" in report and RECEIPT in report
        assert all(term not in report for term in (
            "DOC-2", unused_receipt, "미사용 공시", "기준금리", "종목 뉴스",
            "총 평가액", "3,557.727272원", "답변 모델"))
        assert any("답변 모델을 사용할 수 없어" in warning for warning in done["guardrail_warnings"]) == outage
        assert any("[응답 시간초과]" in warning for warning in done["guardrail_warnings"]) == outage
