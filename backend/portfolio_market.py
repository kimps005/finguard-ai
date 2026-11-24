"""Prepare one account snapshot for Module 3 using the Module 2, 4 and 5 clients."""

import asyncio
from dataclasses import asdict, dataclass
from datetime import date, timedelta
from decimal import Decimal
import json
import logging

from psycopg.rows import dict_row

from backend import db
from backend.broker import BrokerConnector, DailyBar, FiveDayFlow, Holding
from backend.connectors import ExternalConnectors
from backend.dart_etl import DartETL
from backend.finance_calc import ANNUAL, THIRD, DartReports, LockupRelease
from backend.market_time import KST, confirmed_day
from backend.stock_master import fetch_stocks
from backend.sync_stock_master import Stock


logger = logging.getLogger(__name__)
# Cold-cache ingestion must leave room for the workers within the PRD's 25-second budget.
INGEST_TIMEOUT = 15


@dataclass
class ScoreInputs:
    stock: Stock
    flow: FiveDayFlow
    bars: tuple[DailyBar, ...] = ()
    reports: DartReports | None = None
    releases: list[LockupRelease] | None = None
    events: list[dict] | None = None


async def outstanding_events(corp_code: str, on: date) -> list[dict]:
    async with db.connection(row_factory=dict_row) as conn:
        cursor = await conn.execute(
            "SELECT corp_code, event_type, convertible_shares, conversion_price, "
            "board_resolution_date, exercise_start_date, exercise_end_date, rcept_no "
            "FROM overhang_events WHERE corp_code = %s AND board_resolution_date <= %s "
            "AND (exercise_end_date IS NULL OR exercise_end_date >= %s) "
            "ORDER BY board_resolution_date DESC, id", (corp_code, on, on))
        return await cursor.fetchall()


async def financial_reports(etl: DartETL, corp_code: str, on: date) -> DartReports:
    reports = {}

    async def load(year, period, basis):
        raw = await etl._request("fnlttSinglAcntAll.json", corp_code=corp_code,
                                 bsns_year=str(year), reprt_code=period, fs_div=basis)
        reports[year, period, basis] = json.loads(raw, parse_float=Decimal)

    for year in (on.year - 1, on.year - 2):
        for basis in ("CFS", "OFS"):
            async with asyncio.TaskGroup() as group:
                for period in (ANNUAL, THIRD):
                    group.create_task(load(year, period, basis))
            pair = [reports[year, period, basis] for period in (ANNUAL, THIRD)]
            if all(item["status"] == "000" and item["list"] for item in pair):
                return reports
        if on.month > 3 or any(reports[year, ANNUAL, div]["status"] != "013"
                               for div in ("CFS", "OFS")):
            break
    return reports


class MarketData:
    def __init__(self, broker: BrokerConnector, etl: DartETL, external: ExternalConnectors):
        self.broker = broker
        self.etl = etl
        self.external = external

    async def collect(self):
        """Gather one snapshot; a failing source degrades its holding instead of the diagnosis.

        Only the balance and stock_master lookups are fatal. Every other source records a
        Korean warning and leaves its score input unknown, so the scorecard leaves that part
        (and the total) unset rather than guessing.
        """
        now = self.broker.clock().astimezone(KST)
        on = now.date()
        warnings: list[str] = []

        async def optional(operation, warning, default=None):
            try:
                return await operation
            except Exception as exc:  # CancelledError is not an Exception and still propagates.
                logger.warning("Score input unavailable (%s)", type(exc).__name__)
                warnings.append(warning)
                return default

        async def issuer_data(stock):
            # Module 4 owns the issuer lock, index and correction-safe event upserts. A timed-out
            # ingest keeps the receipts it finished; the next request resumes from there.
            async def ingest():
                async with asyncio.timeout(INGEST_TIMEOUT):
                    await self.etl.ingest_stock(stock.stock_code, on - timedelta(days=90), on)

            await optional(ingest(), f"{stock.corp_name} 최신 공시 적재가 끝나지 않아 저장된 공시로 분석했습니다.")
            return await optional(outstanding_events(stock.corp_code, on),
                                  f"{stock.corp_name} 오버행 이벤트를 불러오지 못해 오버행 점수를 산정하지 않았습니다.")

        async def score_inputs(stock, issuer):
            code, name = stock.stock_code, stock.corp_name
            async with asyncio.TaskGroup() as group:
                flow = group.create_task(optional(
                    self.broker.get_five_day_flow(code, as_of=now, asset_type=stock.asset_type),
                    f"{name} 최근 5거래일 확정 수급을 불러오지 못해 정량 점수에서 제외했습니다."))
                if stock.asset_type == "STOCK":
                    # Both MA windows must end on a completed trading session.
                    end = confirmed_day(now)
                    bars = group.create_task(optional(
                        self.broker.get_daily_bars(code, end - timedelta(days=60), end, asset_type=stock.asset_type),
                        f"{name} 일봉을 불러오지 못해 모멘텀 점수를 산정하지 않았습니다.", ()))
                    events = await issuer if issuer else None
                    lockup = group.create_task(self.external.get_lockup(asdict(stock), as_of=on))
            if flow.result() is None:
                return None
            inputs = ScoreInputs(stock, flow.result())
            if stock.asset_type == "STOCK":
                inputs.bars = bars.result()
                inputs.events = events
                if lockup.result()["status"] == "ok":
                    inputs.releases = lockup.result()["releases"]
            return inputs

        async with asyncio.TaskGroup() as group:
            macro = group.create_task(optional(self.external.get_macro_context(as_of=on),
                                               "환율·금리 자료를 불러오지 못했습니다."))
            balance = await self.broker.get_balance()
            master = await fetch_stocks([item.stock_code for item in balance.holdings])
            issuers, reports, jobs = {}, {}, {}
            for code, stock in master.items():
                corp = stock.corp_code
                if stock.asset_type == "STOCK" and corp and corp not in issuers:
                    issuers[corp] = group.create_task(issuer_data(stock))
                    reports[corp] = group.create_task(optional(
                        financial_reports(self.etl, corp, on),
                        f"{stock.corp_name} 재무제표를 불러오지 못해 실적 점수를 산정하지 않았습니다."))
                jobs[code] = group.create_task(score_inputs(stock, issuers.get(corp)))
        inputs = {code: job.result() for code, job in jobs.items() if job.result() is not None}
        for item in inputs.values():
            if item.stock.corp_code in reports:
                item.reports = reports[item.stock.corp_code].result()
        return balance, on, macro.result(), inputs, list(dict.fromkeys(warnings))

    async def orderable_cash(self, holding: Holding | None = None) -> Decimal:
        return await self.broker.get_orderable_cash(holding)
