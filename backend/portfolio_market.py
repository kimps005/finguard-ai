"""Prepare one account snapshot for Module 3 using the Module 2, 4 and 5 clients."""

import asyncio
from dataclasses import asdict, dataclass
from datetime import date, timedelta
from decimal import Decimal
import json

import psycopg
from psycopg.rows import dict_row

from backend.broker import BrokerConnector, DailyBar, FiveDayFlow, Holding
from backend.connectors import ExternalConnectors
from backend.dart_etl import DartETL
from backend.finance_calc import ANNUAL, THIRD, DartReports, LockupRelease
from backend.market_time import KST, confirmed_day
from backend.sync_stock_master import Stock


@dataclass
class ScoreInputs:
    stock: Stock
    flow: FiveDayFlow
    bars: tuple[DailyBar, ...] = ()
    reports: DartReports | None = None
    releases: list[LockupRelease] | None = None
    events: list[dict] | None = None


async def stock_master(codes: list[str]) -> dict[str, Stock]:
    if not codes:
        return {}
    async with await psycopg.AsyncConnection.connect(row_factory=dict_row) as conn:
        cursor = await conn.execute(
            "SELECT stock_code, corp_code, isin_cd, crno, corp_name, asset_type, induty_code, "
            "listed_shares::numeric FROM stock_master WHERE stock_code = ANY(%s)", (codes,))
        return {row["stock_code"]: Stock(**row) for row in await cursor.fetchall()}


async def search_stocks(query: str) -> list[dict]:
    if not query:
        return []
    async with await psycopg.AsyncConnection.connect(row_factory=dict_row) as conn:
        cursor = await conn.execute(
            """SELECT stock_code, corp_name, market
               FROM stock_master
               WHERE stock_code = upper(%(q)s)
                  OR strpos(lower(corp_name), lower(%(q)s)) > 0
               ORDER BY CASE WHEN stock_code = upper(%(q)s) THEN 0
                             WHEN lower(corp_name) = lower(%(q)s) THEN 1
                             WHEN starts_with(lower(corp_name), lower(%(q)s)) THEN 2
                             ELSE 3 END,
                        length(corp_name), stock_code
               LIMIT 8""", {"q": query})
        return await cursor.fetchall()


async def outstanding_events(corp_code: str, on: date) -> list[dict]:
    async with await psycopg.AsyncConnection.connect(row_factory=dict_row) as conn:
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
        now = self.broker.clock().astimezone(KST)
        on = now.date()

        async def issuer_data(stock):
            # Module 4 owns the issuer lock, index and correction-safe event upserts.
            await self.etl.ingest_stock(stock.stock_code, on - timedelta(days=90), on)
            return await outstanding_events(stock.corp_code, on)

        async def score_inputs(stock, issuer):
            code = stock.stock_code
            async with asyncio.TaskGroup() as group:
                flow = group.create_task(self.broker.get_five_day_flow(
                    code, as_of=now, asset_type=stock.asset_type))
                if stock.asset_type == "STOCK":
                    # Both MA windows must end on a completed trading session.
                    end = confirmed_day(now)
                    bars = group.create_task(self.broker.get_daily_bars(
                        code, end - timedelta(days=60), end, asset_type=stock.asset_type))
                    events = await issuer if issuer else None
                    lockup = group.create_task(self.external.get_lockup(asdict(stock), as_of=on))
            inputs = ScoreInputs(stock, flow.result())
            if stock.asset_type == "STOCK":
                inputs.bars = bars.result()
                inputs.events = events
                if lockup.result()["status"] == "ok":
                    inputs.releases = lockup.result()["releases"]
            return inputs

        async with asyncio.TaskGroup() as group:
            macro = group.create_task(self.external.get_macro_context(as_of=on))
            balance = await self.broker.get_balance()
            master = await stock_master([item.stock_code for item in balance.holdings])
            issuers, reports, jobs = {}, {}, {}
            for code, stock in master.items():
                corp = stock.corp_code
                if stock.asset_type == "STOCK" and corp and corp not in issuers:
                    issuers[corp] = group.create_task(issuer_data(stock))
                    reports[corp] = group.create_task(financial_reports(self.etl, corp, on))
                jobs[code] = group.create_task(score_inputs(stock, issuers.get(corp)))
        inputs = {code: job.result() for code, job in jobs.items()}
        for item in inputs.values():
            if item.stock.corp_code in reports:
                item.reports = reports[item.stock.corp_code].result()
        return balance, on, macro.result(), inputs

    async def orderable_cash(self, holding: Holding | None = None) -> Decimal:
        return await self.broker.get_orderable_cash(holding)
