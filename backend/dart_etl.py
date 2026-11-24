"""Ingest DART filings for one stock using Module 1's issuer mapping."""

import argparse
import asyncio
from contextlib import suppress
from datetime import date
from decimal import Decimal
from io import BytesIO
import json
import logging
from pathlib import Path
import re
from zipfile import ZipFile

import httpx
import psycopg
from redis.exceptions import LockNotOwnedError

from backend import dart_rag, db
from backend.dart_chunks import chunk_document


DART_API = "https://opendart.fss.or.kr/api/"
# endpoint, shares, price, exercise-period prefix as published by OpenDART DS005
BONDS = {
    "CB": ("cvbdIsDecsn.json", "cvisstk_cnt", "cv_prc", "cvrqpd"),
    "BW": ("bdwtIsDecsn.json", "nstk_isstk_cnt", "ex_prc", "expd"),
    "EB": ("exbdIsDecsn.json", "extg_stkcnt", "ex_prc", "exrqpd"),
}
UPSERT_EVENT = """
INSERT INTO overhang_events (corp_code, bond_type_issue_no, board_resolution_date,
    event_type, convertible_shares, conversion_price, exercise_start_date, exercise_end_date, rcept_no)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (corp_code, bond_type_issue_no, board_resolution_date) DO UPDATE SET
    event_type = EXCLUDED.event_type, convertible_shares = EXCLUDED.convertible_shares,
    conversion_price = EXCLUDED.conversion_price, exercise_start_date = EXCLUDED.exercise_start_date,
    exercise_end_date = EXCLUDED.exercise_end_date, rcept_no = EXCLUDED.rcept_no,
    updated_at = CURRENT_TIMESTAMP
WHERE overhang_events.rcept_no <= EXCLUDED.rcept_no
"""
INSERT_CHUNK = """
INSERT INTO dart_chunks (corp_code, rcept_no, chunk_no, report_nm, rcept_dt,
    chunk_text, embedding, search_tsv)
VALUES (%s, %s, %s, %s, %s, %s, %s::vector, to_tsvector('simple', %s))
"""
LOCK_SECONDS = 600
logger = logging.getLogger(__name__)


def _optional(value):
    value = str(value).strip() if value is not None else ""
    return None if value in {"", "-", "해당없음"} else value


def _date(value):
    value = _optional(value)
    if value is None:
        return None
    match = re.fullmatch(r"(\d{4})\s*[-./년]\s*(\d{1,2})\s*[-./월]\s*(\d{1,2})\s*[일.]?", value)
    if match:
        return date(*map(int, match.groups()))
    return date.fromisoformat(value)


def _receipt(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{14}", value):
        raise ValueError("DART receipt must have 14 digits")
    return value


def parse_event(corp_code: str, kind: str, row: dict) -> tuple:
    _, share_field, price_field, period = BONDS[kind]
    if row["corp_code"] != corp_code:
        raise ValueError("DART returned a different issuer")
    board_date = _date(row["bddd"])
    if board_date is None:
        raise ValueError("missing board resolution date")
    shares = Decimal(str(row[share_field]).replace(",", ""))
    if not shares.is_finite() or shares != shares.to_integral_value() or not 0 <= shares < 2**63:
        raise ValueError("invalid convertible share count")
    price_text = _optional(row.get(price_field))
    price = Decimal(price_text.replace(",", "")) if price_text is not None else None
    if price is not None and (not price.is_finite() or price < 0 or price >= 10**16
                              or price != price.quantize(Decimal("0.01"))):
        raise ValueError("price outside NUMERIC(18,2)")
    return (corp_code, _optional(row.get("bd_tm")), board_date, kind, int(shares), price,
            _date(row.get(period + "_bgd")), _date(row.get(period + "_edd")), _receipt(row["rcept_no"]))


def _document_chunks(archive: bytes) -> list[str]:
    chunks = []
    with ZipFile(BytesIO(archive)) as package:
        for name in package.namelist():
            if name.lower().endswith((".xml", ".html", ".htm")):
                data = package.read(name)
                try:
                    markup = data.decode("utf-8-sig")
                except UnicodeDecodeError:
                    markup = data.decode("cp949")
                chunks.extend(chunk_document(markup))
    if not chunks:
        raise ValueError("DART archive contains no document text")
    return chunks


class DartETL:
    def __init__(self, redis, http, api_key):
        self.redis = redis
        self.http = http
        self.api_key = api_key
        self._pending_ingests: dict[str, asyncio.Task] = {}

    async def has_recent_chunks(self, corp_code: str, start: date, end: date) -> bool:
        async with db.connection() as conn:
            cursor = await conn.execute(
                "SELECT 1 FROM dart_chunks WHERE corp_code = %s AND rcept_dt BETWEEN %s AND %s LIMIT 1",
                (corp_code, start, end))
            return await cursor.fetchone() is not None

    async def ingest_missing_stock(self, stock_code: str, corp_code: str, start: date, end: date):
        if not self.api_key or await self.has_recent_chunks(corp_code, start, end):
            return

        async def ingest():
            try:
                async with asyncio.timeout(LOCK_SECONDS):
                    await self.ingest_stock(stock_code, start, end)
            except Exception as exc:
                logger.warning("On-demand DART ingestion failed (%s)", type(exc).__name__)
            finally:
                self._pending_ingests.pop(corp_code, None)

        task = self._pending_ingests.get(corp_code)
        if task is None:
            task = self._pending_ingests[corp_code] = asyncio.create_task(ingest())
        # A short-lived SSE request can finish while the issuer continues indexing.
        await asyncio.shield(task)

    async def close(self):
        tasks = tuple(self._pending_ingests.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _request(self, endpoint, **params):
        if not self.api_key:
            raise ValueError("DART_API_KEY is required")
        try:
            response = await self.http.get(DART_API + endpoint, timeout=30,
                                           params={"crtfc_key": self.api_key, **params})
        except httpx.RequestError:
            raise RuntimeError(f"DART request failed for {endpoint}") from None
        if not response.is_success:
            raise RuntimeError(f"DART {endpoint} returned HTTP {response.status_code}")
        return response.content

    async def _list(self, endpoint, **params):
        payload = json.loads(await self._request(endpoint, **params), parse_float=Decimal)
        if payload["status"] == "013":
            return [], 0
        if payload["status"] != "000":
            raise RuntimeError(f"DART {endpoint} returned status {payload['status']}")
        return payload["list"], int(payload.get("total_page", 1))

    async def ingest_stock(self, stock_code: str, start: date, end: date) -> bool:
        """Return False for excluded/unmapped securities or an already running issuer."""
        if start > end:
            raise ValueError("start must precede end")
        async with db.connection() as conn:
            cursor = await conn.execute(
                "SELECT corp_code FROM stock_master WHERE stock_code = %s "
                "AND asset_type = 'STOCK' AND corp_code IS NOT NULL", (stock_code,))
            issuer = await cursor.fetchone()
        if not issuer:
            return False
        corp_code, = issuer
        # redis-py uses SET NX PX and token-checked Lua for extension and release.
        lock = self.redis.lock(f"etl_lock:{corp_code}", timeout=LOCK_SECONDS, thread_local=False)
        if not await lock.acquire(blocking=False):
            return False
        try:
            async with asyncio.TaskGroup() as tasks:
                renewal = tasks.create_task(self._renew(lock))
                try:
                    await self._ingest(corp_code, start, end)
                finally:
                    renewal.cancel()
        finally:
            with suppress(LockNotOwnedError):
                await lock.release()
        return True

    @staticmethod
    async def _renew(lock):
        while True:
            await asyncio.sleep(LOCK_SECONDS // 3)
            await lock.reacquire()

    async def _ingest(self, corp_code, start, end):
        params = {"corp_code": corp_code, "bgn_de": start.strftime("%Y%m%d"),
                  "end_de": end.strftime("%Y%m%d")}
        events = []
        for kind, (endpoint, *_) in BONDS.items():
            rows, _ = await self._list(endpoint, **params)
            events.extend(parse_event(corp_code, kind, row) for row in rows)
        if events:
            async with db.connection() as conn, conn.cursor() as cursor:
                await cursor.executemany(UPSERT_EVENT, events)

        page = 1
        while True:
            reports, pages = await self._list("list.json", **params, page_no=page, page_count=100)
            for report in reports:
                if report["corp_code"] != corp_code:
                    raise ValueError("DART returned a filing from a different issuer")
                await self._index_report(corp_code, report)
            if page >= pages:
                break
            page += 1

    async def _index_report(self, corp_code, report):
        receipt = _receipt(report["rcept_no"])
        async with db.connection() as conn:
            cursor = await conn.execute("SELECT 1 FROM dart_chunks WHERE rcept_no = %s LIMIT 1", (receipt,))
            if await cursor.fetchone():
                return  # Receipts are immutable; a correction has its own receipt.
        data = await self._request("document.xml", rcept_no=receipt)
        chunks = await asyncio.to_thread(_document_chunks, data)
        report_date = date.fromisoformat(report["rcept_dt"])
        # One transaction per receipt. A failed batch leaves no partial searchable filing.
        async with db.connection() as conn, conn.cursor() as cursor:
            for start in range(0, len(chunks), dart_rag.BATCH_SIZE):
                batch = chunks[start:start + dart_rag.BATCH_SIZE]
                vectors = await dart_rag.embed(batch)
                rows = []
                for i, (text, vector) in enumerate(zip(batch, vectors, strict=True), start):
                    tokens = await dart_rag.keywords(text)
                    rows.append((corp_code, receipt, i, report["report_nm"], report_date,
                                 text, json.dumps(vector, allow_nan=False), " ".join(tokens)))
                await cursor.executemany(INSERT_CHUNK, rows)


def migrate():
    migrations = Path(__file__).with_name("migrations")
    with psycopg.connect(autocommit=True) as conn:
        for name in ("004_module4.sql", "005_stock_market.sql"):
            conn.execute((migrations / name).read_text(encoding="utf-8"))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--migrate", required=True, action="store_true")
    parser.parse_args()
    migrate()
