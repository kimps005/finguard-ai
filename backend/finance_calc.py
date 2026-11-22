"""Module 3. Pure calculations on a single portfolio snapshot, with no external I/O."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Context, Decimal, ROUND_HALF_UP, localcontext
import re

from backend.broker import Balance, DailyBar, FiveDayFlow, Holding
from backend.sync_stock_master import Stock


ZERO = Decimal(0)
HUNDRED = Decimal(100)
# Preserve intermediate digits (including products of two BIGINT-sized inputs).
CALC_CONTEXT = Context(prec=64, rounding=ROUND_HALF_UP)


def _decimal(value: Decimal | int | str) -> Decimal:
    if isinstance(value, str):
        value = value.strip().replace(",", "")
    elif type(value) not in (Decimal, int):
        raise TypeError("Expected Decimal, an integer or a decimal string")
    result = Decimal(value)
    if not result.is_finite():
        raise ValueError("Expected a finite number")
    return result


def _nonnegative(value: Decimal | int) -> Decimal:
    result = _decimal(value)
    if result < ZERO:
        raise ValueError("Expected a nonnegative amount")
    return result


def trailing_multiples(current_price: Decimal, listed_shares: Decimal | int,
                       quarterly_net_income: Sequence[Decimal], total_equity: Decimal
                       ) -> tuple[Decimal | None, Decimal | None]:
    """Return PER and PBR. Supply four consecutive, non-cumulative quarterly profits."""
    with localcontext(CALC_CONTEXT):
        shares = _nonnegative(listed_shares)
        cap = _nonnegative(current_price) * shares
        equity = _decimal(total_equity)
        ttm = (sum(map(_decimal, quarterly_net_income), ZERO)
               if len(quarterly_net_income) == 4 else ZERO)
        return (cap / ttm if shares and ttm > ZERO else None,
                cap / equity if shares and equity > ZERO else None)


# One issuer's fnlttSinglAcntAll responses keyed by the original request
# (business year, report code, fs_div). fs_div need not be repeated in the rows.
DartReports = Mapping[tuple[int, str, str], Mapping]
ANNUAL, THIRD = "11011", "11014"


@dataclass(frozen=True)
class Account:
    name: str
    ids: tuple[str, ...]
    korean: re.Pattern


OPERATING = Account("operating_profit", ("ifrs-full_ProfitLossFromOperatingActivities",
                    "dart_OperatingIncomeLoss"), re.compile(r"영업(?:이익|손실|손익)(?:\((?:이익|손실)\))?"))
REVENUE = Account("operating_revenue", ("ifrs-full_Revenue", "dart_OperatingRevenue"),
                  re.compile(r"영업수익(?:\(매출액\))?"))
NET = Account("net_income", ("ifrs-full_ProfitLoss",), re.compile(
    r"(?:당기|분기)순(?:이익|손실|손익)(?:\((?:순)?(?:이익|손실)\))?"))


def _statement_rows(reports, year, report_code, fs_div, corp_code):
    response = reports.get((year, report_code, fs_div))
    if response is None or response["status"] == "013":
        return ()
    if response["status"] != "000":
        raise ValueError(f"DART report unavailable (status {response['status']})")
    for row in response["list"]:
        if (str(row.get("bsns_year", year)) != str(year)
                or row.get("reprt_code", report_code) != report_code
                or row.get("fs_div", fs_div) != fs_div
                or row.get("corp_code", corp_code) != corp_code):
            raise ValueError("DART row does not match its issuer or reporting period")
    return response["list"]


def _account_amount(rows, account, field):
    """Choose an identified line first. Conflicting equally ranked lines are ambiguous."""
    matches = {}
    for row in rows:
        if row.get("sj_div") not in ("IS", "CIS") or row.get(field) in (None, "", "-"):
            continue
        if row.get("account_id") in account.ids:
            rank = 0
        elif account.korean.fullmatch("".join(row.get("account_nm", "").split())):
            rank = 1
        else:
            continue
        matches.setdefault(rank, set()).add(_decimal(row[field]))
    if matches:
        values = matches[min(matches)]
        if len(values) == 1:
            return next(iter(values))
    return None


@dataclass(frozen=True)
class QuarterComparison:
    fiscal_year: int
    fs_div: str
    account: str | None
    q4: Decimal | None
    q3: Decimal | None


def _financial_sector(stock: Stock) -> bool:
    return (stock.induty_code or "").strip().removeprefix("K").startswith(("64", "65", "66"))


def fourth_quarter(stock: Stock, reports: DartReports, as_of: date) -> QuarterComparison | None:
    """Compare Q4 to standalone Q3, falling back one fiscal year only on Q1 annual 013."""
    year = as_of.year - 1
    annual_statuses = {reports[year, ANNUAL, div]["status"] for div in ("CFS", "OFS")
                       if (year, ANNUAL, div) in reports}
    if as_of.month <= 3 and annual_statuses == {"013"}:
        year -= 1

    with localcontext(CALC_CONTEXT):
        for div in ("CFS", "OFS"):
            annual = _statement_rows(reports, year, ANNUAL, div, stock.corp_code)
            third = _statement_rows(reports, year, THIRD, div, stock.corp_code)
            if not annual or not third:
                continue
            partial = QuarterComparison(year, div, None, None, None)
            for account in ((REVENUE, NET) if _financial_sector(stock) else (OPERATING,)):
                full = _account_amount(annual, account, "thstrm_amount")
                ytd = _account_amount(third, account, "thstrm_add_amount")
                if full is not None and ytd is not None:
                    q3 = _account_amount(third, account, "thstrm_amount")
                    comparison = QuarterComparison(year, div, account.name, full - ytd, q3)
                    if q3 is not None:
                        return comparison
                    if partial.q4 is None:
                        partial = comparison
            return partial
    return None


def earnings_score(stock: Stock, comparison: QuarterComparison | None) -> int | None:
    if comparison is None or comparison.q4 is None or comparison.q3 is None:
        return 15 if _financial_sector(stock) and comparison is not None else None
    current, previous = comparison.q4, comparison.q3
    if current > ZERO and previous <= ZERO:
        return 20
    if current > previous:
        return 25 if current > ZERO else 10
    return 0


@dataclass(frozen=True)
class LockupRelease:
    isin_cd: str
    release_date: date
    shares: Decimal


def overhang_safety(stock: Stock, as_of: date, releases: Sequence[LockupRelease],
                    overhang_events: Sequence[Mapping]) -> tuple[Decimal | None, int | None]:
    """Use FSC releases through day 30 inclusive and outstanding issuer-level DB rows."""
    with localcontext(CALC_CONTEXT):
        shares = _nonnegative(stock.listed_shares)
        if not shares:
            return None, None
        end = as_of + timedelta(days=30)
        locked = sum((_nonnegative(row.shares) for row in releases
                      if row.isin_cd == stock.isin_cd and as_of <= row.release_date <= end), ZERO)
        convertible = sum((_nonnegative(row["convertible_shares"]) for row in overhang_events
                           if row["corp_code"] == stock.corp_code
                           and row["board_resolution_date"] <= as_of
                           and (row.get("exercise_end_date") or date.max) >= as_of), ZERO)
        pct = (locked + convertible) * HUNDRED / shares
        if pct == ZERO:
            return pct, 25
        if pct < 3:
            return pct, 15
        return pct, 5 if pct < 5 else 0


def flow_score(flow: FiveDayFlow) -> int:
    """Slowing sales: last two sessions' combined daily mean exceeds the first three's."""
    if len(flow.days) != 5 or any(a.session >= b.session for a, b in zip(flow.days, flow.days[1:])):
        raise ValueError("Expected five confirmed sessions in chronological order")
    with localcontext(CALC_CONTEXT):
        foreign, institution = _decimal(flow.foreign_net_buy), _decimal(flow.institution_net_buy)
        if foreign > ZERO and institution > ZERO:
            return 25
        if foreign > ZERO or institution > ZERO:
            return 15
        daily = [_decimal(d.foreign_net_buy) + _decimal(d.institution_net_buy) for d in flow.days]
        early, recent = sum(daily[:3], ZERO), sum(daily[3:], ZERO)
        return 5 if early < ZERO and recent * 3 > early * 2 else 0


def momentum_score(current_price: Decimal, bars: Sequence[DailyBar]) -> int | None:
    """Use adjacent completed MA20 windows. Two consecutive below-MA readings score zero."""
    if len(bars) < 21:
        return None
    window = bars[-21:]
    if any(a.session >= b.session for a, b in zip(window, window[1:])):
        raise ValueError("Expected distinct bars in chronological order")
    with localcontext(CALC_CONTEXT):
        closes = [_nonnegative(bar.close) for bar in window]
        previous = sum(closes[:-1], ZERO) / 20
        current = sum(closes[1:], ZERO) / 20
        if previous == ZERO:
            return None
        if _nonnegative(current_price) <= current:
            return 0 if current_price < current and closes[-2] < previous else 10
        slope_pct = (current - previous) * HUNDRED / previous
        if abs(slope_pct) <= Decimal("0.05"):
            return 15
        return 25 if slope_pct > ZERO else 10


def calculate_scorecard(stock: Stock, holding: Holding, flow: FiveDayFlow, *, as_of: date,
                        reports: DartReports | None = None, bars: Sequence[DailyBar] = (),
                        releases: Sequence[LockupRelease] | None = None,
                        overhang_events: Sequence[Mapping] | None = None) -> dict:
    """Return the payload for quant_scorecards[stock_code]. Unknown inputs leave total unset."""
    if stock.stock_code != holding.stock_code:
        raise ValueError("Holding does not match stock_master")
    if stock.asset_type != "STOCK":
        return {"current_price": holding.current_price, "average_price": holding.average_price,
                "profit_loss_pct": holding.profit_loss_pct,
                "five_day_flow": {"as_of": flow.as_of, "foreign_net_buy": flow.foreign_net_buy,
                                  "institution_net_buy": flow.institution_net_buy}}

    quarter = fourth_quarter(stock, reports, as_of) if reports is not None else None
    dilution, safety = (overhang_safety(stock, as_of, releases, overhang_events)
                        if releases is not None and overhang_events is not None else (None, None))
    scores = {"earnings": earnings_score(stock, quarter), "overhang": safety,
              "flow": flow_score(flow), "momentum": momentum_score(holding.current_price, bars)}
    return {"scores": scores, "total": sum(scores.values()) if None not in scores.values() else None,
            "earnings_comparison": quarter, "dilution_pct": dilution}


@dataclass(frozen=True)
class PurchaseSimulation:
    buy_quantity: Decimal
    invested_amount: Decimal
    average_price: Decimal | None
    profit_loss_pct: Decimal | None
    profit_loss_change_pp: Decimal | None
    weight: Decimal | None
    weight_change: Decimal | None
    remaining_cash: Decimal


def simulate_purchase(holding: Holding, balance: Balance, orderable_cash: Decimal, *,
                      ratio: Decimal | None = None, amount: Decimal | None = None) -> PurchaseSimulation:
    """Fee-free purchase at the snapshot price. Ratio and portfolio weights are fractions.

    Supply orderable cash separately from balance.cash_deposit. Use broker book cost so
    a rounded average price cannot change the cost basis. Returns use percentage points.
    """
    with localcontext(CALC_CONTEXT):
        cash = _nonnegative(orderable_cash)
        if (ratio is None) == (amount is None):
            raise ValueError("Specify exactly one of ratio or amount")
        if ratio is not None:
            fraction = _nonnegative(ratio)
            if fraction > 1:
                raise ValueError("Investment ratio exceeds one")
            budget = cash * fraction
        else:
            budget = _nonnegative(amount)
        if budget > cash:
            raise ValueError("Investment amount exceeds orderable cash")
        price = _nonnegative(holding.current_price)
        if not price:
            raise ValueError("Purchase price must be positive")
        quantity, cost = _nonnegative(holding.quantity), _nonnegative(holding.purchase_amount)
        total = _nonnegative(balance.total_value) if balance.cash_deposit is not None else None
        buy = budget // price
        spent = buy * price
        new_quantity, new_cost = quantity + buy, cost + spent
        value = new_quantity * price
        profit = quantity * price - cost
        before_pct = profit * HUNDRED / cost if cost else None
        after_pct = profit * HUNDRED / new_cost if new_cost else None
        return PurchaseSimulation(
            buy, spent, new_cost / new_quantity if new_quantity else None, after_pct,
            after_pct - before_pct if before_pct is not None and after_pct is not None else None,
            value / total if total else None, spent / total if total else None, cash - spent,
        )
