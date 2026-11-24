import asyncio
from datetime import date
from decimal import Decimal
import json
from unittest.mock import AsyncMock

from backend.dart_etl import DartETL
from backend.finance_calc import ANNUAL, THIRD
from backend.portfolio_market import financial_reports


COMPLETE = {"status": "000", "list": [{"account_id": "dart_OperatingIncomeLoss"}]}
MISSING = {"status": "013", "list": []}


class Statements:
    """DART double: responses keyed by (year, basis); records every request."""

    def __init__(self, available):
        self.available = available
        self.calls = []

    async def financial_statement(self, corp_code, year, period, basis):
        self.calls.append((year, period, basis))
        return self.available.get((year, basis), MISSING)


def run(on, available):
    etl = Statements(available)
    reports = asyncio.run(financial_reports(etl, "00126380", on))
    return reports, sorted(etl.calls)


def test_consolidated_statements_stop_the_search():
    reports, calls = run(date(2026, 5, 1), {(2025, "CFS"): COMPLETE})
    assert calls == [(2025, ANNUAL, "CFS"), (2025, THIRD, "CFS")]
    assert set(reports) == {(2025, ANNUAL, "CFS"), (2025, THIRD, "CFS")}


def test_separate_statements_are_used_when_consolidated_are_missing():
    reports, calls = run(date(2026, 5, 1), {(2025, "OFS"): COMPLETE})
    assert calls == sorted((2025, period, basis) for period in (ANNUAL, THIRD) for basis in ("CFS", "OFS"))
    assert reports[2025, ANNUAL, "OFS"] == COMPLETE


def test_before_april_an_unfiled_annual_report_falls_back_one_year():
    reports, calls = run(date(2026, 3, 31), {(2024, "CFS"): COMPLETE})
    assert {year for year, _, _ in calls} == {2024, 2025}
    assert reports[2024, THIRD, "CFS"] == COMPLETE


def test_after_march_or_on_other_errors_there_is_no_older_fallback():
    _, calls = run(date(2026, 4, 1), {(2024, "CFS"): COMPLETE})
    assert {year for year, _, _ in calls} == {2025}
    _, calls = run(date(2026, 2, 1), {(2025, "CFS"): {"status": "020", "list": []}, (2024, "CFS"): COMPLETE})
    assert {year for year, _, _ in calls} == {2025}


def test_dart_statement_request_and_decimal_amounts():
    etl = DartETL(None, None, "key")
    etl._request = AsyncMock(return_value=json.dumps({"status": "000", "list": [{"thstrm_amount": 1.10}]}).encode())
    statement = asyncio.run(etl.financial_statement("00126380", 2025, ANNUAL, "CFS"))
    etl._request.assert_awaited_once_with("fnlttSinglAcntAll.json", corp_code="00126380", bsns_year="2025",
                                          reprt_code=ANNUAL, fs_div="CFS")
    assert statement["list"][0]["thstrm_amount"] == Decimal("1.10")
