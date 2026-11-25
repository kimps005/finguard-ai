from datetime import date
from decimal import Decimal as D
from types import SimpleNamespace

import pytest

from backend.report import diagnosis_report


QUESTION = "한온시스템 추가매수할까? 홀드할까? 더 오를 것 같은데 어떻게 해?"


def state(query, *, current="3100", ma20="3168", foreign="750879", institution="1325060", simulation=None):
    holding = {"stock_code": "018880", "name": "한온시스템", "current_price": D(current),
               "average_price": D("3557.727272"), "weight": D("0.5")}
    bars = tuple(SimpleNamespace(close=D(ma20)) for _ in range(20))
    flow = SimpleNamespace(foreign_net_buy=D(foreign), institution_net_buy=D(institution))
    return {"user_query": query, "simulation_result": simulation, "holdings": [holding],
            "stock_analysis_results": [], "quant_scorecards": {},
            "score_inputs": {"018880": SimpleNamespace(bars=bars, flow=flow, events=[])},
            "as_of": date(2026, 11, 20)}


def first_paragraph(report):
    return report.split("\n\n")[1]


@pytest.mark.parametrize("kwargs,expected", [
    ({}, ("추가매수 조건은 아직 충족되지 않았습니다", "보유", "20일선 3,168원 회복")),
    ({"current": "3300"}, ("추가매수를 검토할 조건이 충족됐습니다", "3,168원을 다시 밑돌면")),
    ({"foreign": "-10", "institution": "-20"}, ("함께 순매도", "하방 위험을 재평가")),
    ({"current": "3300", "foreign": "-10"}, ("일부만 충족", "보유")),
])
def test_decision_question_is_answered_first(kwargs, expected):
    answer = first_paragraph(diagnosis_report(state(QUESTION, **kwargs)))
    assert answer.startswith("**한온시스템** 추가매수·보유 판단: ")
    assert all(term in answer for term in expected)
    # The local figures that back the verdict still follow it.
    assert "현재가 3," in answer and "최근 5거래일 순매수" in answer


def test_decision_answer_precedes_a_simulation_prompt():
    report = diagnosis_report(state(QUESTION, simulation={
        "message": "사용할 주문 가능 현금의 비율(%) 또는 투자 금액(원)을 한 개 지정해 주세요."}))
    assert first_paragraph(report).startswith("**한온시스템** 추가매수·보유 판단: ")
    assert "투자 금액(원)을 한 개 지정해 주세요" in report


def test_plain_diagnosis_has_no_decision_verdict():
    report = diagnosis_report(state("한온시스템 공시 위험을 진단해 줘"))
    assert "추가매수·보유 판단" not in report
    assert first_paragraph(report).startswith("**한온시스템** 현재가 3,100원")
