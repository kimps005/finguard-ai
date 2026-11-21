from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal as D, ROUND_DOWN, localcontext

import pytest

from backend import finance_calc as calc
from backend.broker import Balance, DailyBar, FiveDayFlow, Holding, InvestorFlow
from backend.sync_stock_master import Stock


AS_OF = date(2026, 3, 31)
STOCK = Stock("005930", "00126380", "KR7005930003", None, "삼성전자", "STOCK", "264",
              D(10000))
HOLDING = Holding(STOCK.stock_code, STOCK.corp_name, STOCK.asset_type, STOCK.corp_code,
                  STOCK.isin_cd, D(10), D(150), D(125), D(1500), D(1250), D(-250),
                  D("-16.67"), D("0.125"))
BALANCE = Balance(D(3000), D(10000), D("0.3"), (HOLDING,))


def line(amount, cumulative=None, *, tag="dart_OperatingIncomeLoss", name="표준 태그", sj="IS"):
    return {"account_id": tag, "account_nm": name, "sj_div": sj, "thstrm_amount": amount,
            "thstrm_add_amount": cumulative}


def statements(annual, third, *, year=2025, div="CFS"):
    def report(code, rows):
        return {"status": "000", "list": [dict(row, bsns_year=str(year), reprt_code=code,
                                               corp_code=STOCK.corp_code) for row in rows]}

    return {(year, calc.ANNUAL, div): report(calc.ANNUAL, annual),
            (year, calc.THIRD, div): report(calc.THIRD, third)}


def flows(foreign=(1, 2, 3, 4, 5), institution=(1, 1, 1, 1, 1)):
    days = tuple(InvestorFlow(AS_OF - timedelta(days=4-i), D(f), D(n))
                 for i, (f, n) in enumerate(zip(foreign, institution)))
    return FiveDayFlow(days, sum((r.foreign_net_buy for r in days), D(0)),
                       sum((r.institution_net_buy for r in days), D(0)))


def bars(last="101", penultimate="100"):
    closes = [D(100)] * 19 + [D(penultimate), D(last)]
    return tuple(DailyBar(AS_OF - timedelta(days=20-i), c, c, c, c, D(10))
                 for i, c in enumerate(closes))


def test_mixed_reports_use_only_ofs_and_xbrl_wins_over_name():
    misleading = line("999", "888", name="영업이익", tag="custom")
    annual = line("1,200", tag="ifrs-full_ProfitLossFromOperatingActivities", sj="CIS")
    reports = statements([misleading, annual], [misleading, line("200", "850")], div="OFS")
    reports[2025, calc.ANNUAL, "CFS"] = {"status": "000", "list": [line("9999")]}
    reports[2025, calc.THIRD, "CFS"] = {"status": "013"}
    q4 = calc.fourth_quarter(STOCK, reports, AS_OF)
    assert q4 == calc.QuarterComparison(2025, "OFS", "operating_profit", D(350), D(200))
    assert calc.earnings_score(STOCK, q4) == 25
    del reports[2025, calc.ANNUAL, "OFS"]
    assert calc.fourth_quarter(STOCK, reports, AS_OF) is None


def test_cfs_priority_korean_fallback_and_no_quarterly_as_cumulative():
    annual = line("33", tag="custom", name="영업 이익(손실)")
    third = line("-7", "30", tag="custom", name="영업손실")
    reports = statements([annual, line("999", sj="CF")], [third])
    reports.update(statements([line("999")], [line("4", "20")], div="OFS"))
    q4 = calc.fourth_quarter(STOCK, reports, AS_OF)
    assert (q4.fs_div, q4.q4, q4.q3) == ("CFS", D(3), D(-7))
    del reports[2025, calc.THIRD, "CFS"]["list"][0]["thstrm_add_amount"]
    assert calc.fourth_quarter(STOCK, reports, AS_OF).q4 is None


@pytest.mark.parametrize("month", [1, 2, 3, 4])
def test_annual_013_falls_back_to_complete_older_pair_only_in_q1(month):
    reports = statements([line("150")], [line("8", "110")], year=2024)
    reports[2025, calc.ANNUAL, "CFS"] = {"status": "013"}
    reports[2025, calc.THIRD, "CFS"] = {"status": "000", "list": [line("20", "999")]}
    value = calc.fourth_quarter(STOCK, reports, date(2026, month, 1))
    if month <= 3:
        assert value == calc.QuarterComparison(2024, "CFS", "operating_profit", D(40), D(8))
    else:
        assert value is None


def test_013_for_cfs_does_not_hide_available_ofs_or_service_error():
    reports = statements([line("150")], [line("8", "110")], div="OFS")
    reports[2025, calc.ANNUAL, "CFS"] = {"status": "013"}
    assert calc.fourth_quarter(STOCK, reports, AS_OF).fs_div == "OFS"
    reports[2025, calc.ANNUAL, "OFS"] = {"status": "020"}
    with pytest.raises(ValueError, match="020"):
        calc.fourth_quarter(STOCK, reports, AS_OF)


@pytest.mark.parametrize("field,wrong", [("bsns_year", "2024"), ("reprt_code", "11013"),
                                         ("fs_div", "OFS"), ("corp_code", "12345678")])
def test_mismatched_dart_row_rejected(field, wrong):
    reports = statements([line("150")], [line("8", "110")])
    reports[2025, calc.THIRD, "CFS"]["list"][0][field] = wrong
    with pytest.raises(ValueError, match="issuer or reporting period"):
        calc.fourth_quarter(STOCK, reports, AS_OF)


@pytest.mark.parametrize("code,tag,expected", [
    ("64121", "ifrs-full_Revenue", 25), ("651", "ifrs-full_ProfitLoss", 25),
    ("66", "dart_OperatingIncomeLoss", 15),
])
def test_financial_sector_accounts_and_neutral_score(code, tag, expected):
    stock = replace(STOCK, induty_code=code)
    reports = statements([line("100", tag=tag)], [line("20", "60", tag=tag)])
    result = calc.calculate_scorecard(stock, HOLDING, flows(), as_of=AS_OF, reports=reports,
                                      bars=bars("102"), releases=[], overhang_events=[])
    assert result["scores"]["earnings"] == expected
    assert result["total"] == 75 + expected


def test_financial_accounts_cannot_be_subtracted_from_each_other():
    bank = replace(STOCK, induty_code="64")
    reports = statements([line("100", tag="ifrs-full_Revenue")],
                         [line("20", "60", tag="ifrs-full_ProfitLoss")])
    comparison = calc.fourth_quarter(bank, reports, AS_OF)
    assert comparison.q4 is None
    assert calc.earnings_score(bank, comparison) == 15


def test_financial_sector_uses_net_income_when_revenue_comparison_is_incomplete():
    bank = replace(STOCK, induty_code="641")
    reports = statements([line("100", tag="ifrs-full_Revenue"), line("40", tag="ifrs-full_ProfitLoss")],
                         [line("", "60", tag="ifrs-full_Revenue"), line("-2", "30", tag="ifrs-full_ProfitLoss")])
    comparison = calc.fourth_quarter(bank, reports, AS_OF)
    assert comparison == calc.QuarterComparison(2025, "CFS", "net_income", D(10), D(-2))
    assert calc.earnings_score(bank, comparison) == 20


@pytest.mark.parametrize("q4,q3,points", [(20, 10, 25), (1, -1, 20), (-1, -5, 10),
                                         (-5, -5, 0), (-6, -5, 0), (5, 6, 0)])
def test_earnings_tiers(q4, q3, points):
    comparison = calc.QuarterComparison(2025, "CFS", "operating_profit", D(q4), D(q3))
    assert calc.earnings_score(STOCK, comparison) == points


@pytest.mark.parametrize("kind", ["ETF", "ETN", "REIT"])
def test_nonstock_returns_only_broker_summary_before_financial_calculations(kind, monkeypatch):
    def unexpected(*args):
        pytest.fail("Non-stock asset reached stock calculations")

    for func in ("fourth_quarter", "earnings_score", "overhang_safety", "flow_score", "momentum_score"):
        monkeypatch.setattr(calc, func, unexpected)
    result = calc.calculate_scorecard(replace(STOCK, asset_type=kind), replace(HOLDING, asset_type=kind),
                                      flows(), as_of=AS_OF, reports={}, releases=[], overhang_events=[])
    assert result == {"current_price": D(125), "average_price": D(150), "profit_loss_pct": D("-16.67"),
                      "five_day_flow": {"as_of": AS_OF, "foreign_net_buy": D(15),
                                        "institution_net_buy": D(5)}}


@pytest.mark.parametrize("locked,convertible,pct,points", [
    (0, 0, "0", 25), (100, 199, "2.99", 15), (100, 200, "3", 5),
    (100, 399, "4.99", 5), (100, 400, "5", 0),
])
def test_overhang_dates_issuer_isin_and_exact_thresholds(locked, convertible, pct, points):
    releases = [calc.LockupRelease(STOCK.isin_cd, AS_OF + timedelta(days=n), D(shares))
                for n, shares in [(-1, 999), (0, 0), (30, locked), (31, 999)]]
    releases.append(calc.LockupRelease("OTHER", AS_OF, D(999)))
    row = {"corp_code": STOCK.corp_code, "convertible_shares": convertible,
           "board_resolution_date": AS_OF, "exercise_start_date": None, "exercise_end_date": None}
    events = [row, dict(row, exercise_end_date=AS_OF-timedelta(days=1)), dict(row, corp_code="OTHER"),
              dict(row, board_resolution_date=AS_OF+timedelta(days=1))]
    assert calc.overhang_safety(STOCK, AS_OF, releases, events) == (D(pct), points)
    assert calc.overhang_safety(replace(STOCK, listed_shares=D(0)), AS_OF, [], []) == (None, None)


@pytest.mark.parametrize("foreign,institution,points", [
    ((2, 2, 2, 2, 2), (1, 1, 1, 1, 1), 25), ((-1, -1, -1, -1, -1), (1, 1, 1, 1, 1), 15),
    ((-9, -9, -9, -2, -2), (0, 0, 0, 0, 0), 5), ((-2, -2, -2, -2, -2), (0, 0, 0, 0, 0), 0),
])
def test_confirmed_five_day_flow_tiers(foreign, institution, points):
    assert calc.flow_score(flows(foreign, institution)) == points


@pytest.mark.parametrize("last,points", [("101", 15), ("99", 15), ("101.0000000001", 25),
                                        ("98.9999999999", 10), ("100", 15)])
def test_ma20_exact_slope_boundary(last, points):
    assert calc.momentum_score(D(125), bars(last)) == points


def test_ma20_first_break_sustained_break_and_missing_base():
    assert calc.momentum_score(D(90), bars("90", "110")) == 10
    assert calc.momentum_score(D(90), bars("90", "95")) == 0
    assert calc.momentum_score(D(90), bars()[:20]) is None
    assert calc.momentum_score(D(90), [replace(b, close=D(0)) for b in bars()]) is None


def test_trailing_ratios_and_safe_independent_denominators():
    profits = [D("-25"), D("100"), D("150"), D("275")]
    assert calc.trailing_multiples(D("12.50"), STOCK.listed_shares, profits, D(50000)) == (250, D("2.5"))
    for invalid in (D(0), D(-1)):
        assert calc.trailing_multiples(D(10), 10, [invalid]*4, D(20)) == (None, D(5))
        assert calc.trailing_multiples(D(10), 10, [D(1)]*4, invalid) == (D(25), None)
    assert calc.trailing_multiples(D(10), 0, profits, D(1)) == (None, None)
    assert calc.trailing_multiples(D(10), 10, profits[:3], D(20)) == (None, D(5))
    with pytest.raises(TypeError):
        calc.trailing_multiples(12.5, 10, profits, D(20))


def test_missing_stock_inputs_leave_total_unavailable():
    assert calc.calculate_scorecard(STOCK, HOLDING, flows(), as_of=AS_OF)["total"] is None


@pytest.mark.parametrize("budget", [{"ratio": D("0.40")}, {"amount": D("40.004")}])
def test_simulator_exact_quantities_cost_returns_weights_and_cash(budget):
    # Broker display average is rounded. The exact book cost is authoritative.
    position = replace(HOLDING, quantity=D(4), average_price=D("15.01"), purchase_amount=D("60.03"),
                       current_price=D("10.005"), market_value=D("40.02"))
    with localcontext() as caller:
        caller.prec, caller.rounding = 5, ROUND_DOWN
        result = calc.simulate_purchase(position, BALANCE, D("100.01"), **budget)
        assert (caller.prec, caller.rounding) == (5, ROUND_DOWN)
    assert result.buy_quantity == D(3)
    assert result.invested_amount == D("30.015")
    assert result.remaining_cash == D("69.995")
    assert result.weight == D("0.0070035")
    assert result.weight_change == D("0.0030015")
    with localcontext(calc.CALC_CONTEXT):
        assert result.average_price == D("90.045") / D(7)
        assert result.profit_loss_pct == -D(2001) / D("90.045")
        assert result.profit_loss_change_pp == result.profit_loss_pct + D(2001) / D("60.03")
        assert result.remaining_cash + result.invested_amount == D("100.01")


def test_exact_simulation_with_terminating_expected_values():
    position = replace(HOLDING, quantity=D(4), purchase_amount=D(100), current_price=D(15))
    result = calc.simulate_purchase(position, BALANCE, D(150), ratio=D("0.4"))
    assert result == calc.PurchaseSimulation(D(4), D(60), D(20), D(-25), D(15),
                                             D("0.012"), D("0.006"), D(90))


def test_simulator_zero_orders_zero_denominators_and_invalid_budgets():
    result = calc.simulate_purchase(HOLDING, BALANCE, D(100), amount=D(99))
    assert (result.buy_quantity, result.average_price, result.weight_change) == (D(0), D(150), D(0))
    assert (result.profit_loss_change_pp, result.remaining_cash) == (D(0), D(100))
    empty = replace(HOLDING, quantity=D(0), purchase_amount=D(0))
    result = calc.simulate_purchase(empty, replace(BALANCE, total_value=D(0)), D(0), ratio=D(1))
    assert (result.average_price, result.profit_loss_pct, result.weight) == (None, None, None)
    for kwargs in ({}, {"amount": D(1), "ratio": D(1)}, {"ratio": D("-0.1")},
                   {"amount": D(101)}, {"ratio": D("1.01")}, {"ratio": D("NaN")}):
        with pytest.raises(ValueError):
            calc.simulate_purchase(HOLDING, BALANCE, D(100), **kwargs)
    with pytest.raises(ValueError, match="price"):
        calc.simulate_purchase(replace(HOLDING, current_price=D(0)), BALANCE, D(100), ratio=D(1))
