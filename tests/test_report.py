from decimal import Decimal as D

from backend.report import disclosure_link, quote_parts, won


def test_won_rounds_half_up_to_whole_won():
    assert [won(D(value)) for value in ("9637.5", "9637.49", "-0.5", "1234567")] == [
        "9,638원", "9,637원", "-1원", "1,234,567원"]
    assert won(None) == "자료 없음"


def test_quote_lines_for_kis_and_toss_accounts():
    holding = {"name": "삼성전자", "stock_code": "005930", "quantity": D(3), "current_price": D(100),
               "average_price": D(90), "profit_loss_pct": D("11.11"), "weight": D("0.25")}
    kis = {"account_summary": {"total_value": D(1200), "cash_deposit": D(900), "cash_weight": D("0.75")},
           "holdings": [holding]}
    assert "".join(quote_parts(kis)) == (
        "총 평가액 1,200원 · 예수금 900원 (75.00%)\n"
        "\n- 삼성전자 (005930) · 3주 · 현재가 100원 · 평단가 90원 · 수익률 11.11% · 비중 25.00%")
    # Toss reports no deposit; the line must say so rather than show 0원.
    toss = {**kis, "account_summary": {"total_value": D(1200), "cash_deposit": None, "cash_weight": None}}
    assert next(quote_parts(toss)) == "국내 주식 평가액 1,200원 · 예수금 조회 불가\n"


def test_disclosure_link_points_to_dart_and_collapses_whitespace():
    assert disclosure_link("DOC-1", " 주요사항보고서\n(전환사채) ", "20260416000470") == (
        "[DOC-1](https://dart.fss.or.kr/dsaf001/main.do?rcpNo=20260416000470) · 주요사항보고서 (전환사채)")
