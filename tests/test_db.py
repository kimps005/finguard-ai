import asyncio
import sys

import psycopg
from psycopg.rows import dict_row
import pytest

from backend import db, stock_master
from backend.sync_stock_master import UPSERT


def run(coro):
    if sys.platform == "win32":  # libpq async connections require a selector loop.
        with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
            return runner.run(coro)
    return asyncio.run(coro)


@pytest.fixture
def pooled(database):
    dsn, schema = database
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(f"SET search_path TO {schema}, public")
        conn.execute("TRUNCATE stock_master")
    return f"{dsn} options='-c search_path={schema},public'"


def test_pool_commits_rolls_back_and_resets_connection_state(pooled):
    async def scenario():
        await db.open_pool(pooled, min_size=1, max_size=1)
        try:
            async with db.connection() as conn:
                await conn.execute(UPSERT, ("005930", "00126380", "KR7005930003", None, "삼성전자",
                                            "STOCK", None, 1, None))
            with pytest.raises(RuntimeError):
                async with db.connection() as conn:
                    await conn.execute("UPDATE stock_master SET corp_name = '변경'")
                    raise RuntimeError
            async with db.connection(row_factory=dict_row) as conn:
                await conn.execute("SET LOCAL enable_seqscan = off")
                row = await (await conn.execute("SELECT corp_name FROM stock_master")).fetchone()
            # max_size=1: this is the same physical connection, checked out again.
            async with db.connection() as conn:
                plain = await (await conn.execute("SELECT corp_name FROM stock_master")).fetchone()
                seqscan = await (await conn.execute("SHOW enable_seqscan")).fetchone()
            return row, plain, seqscan, await stock_master.fetch_stocks(["005930"])
        finally:
            await db.close_pool()

    row, plain, seqscan, stocks = run(scenario())
    assert row == {"corp_name": "삼성전자"}  # committed insert, rolled-back update
    assert plain == ("삼성전자",)  # the previous block's dict_row did not leak
    assert seqscan == ("on",)  # SET LOCAL ended with its transaction
    assert stocks["005930"].corp_name == "삼성전자"
    assert db._pool is None


def test_unreachable_database_raises_a_psycopg_error_after_the_acquire_timeout(monkeypatch):
    monkeypatch.setattr(db, "ACQUIRE_TIMEOUT", 0.5)

    async def scenario():
        # Opening must not block application startup while the database is down.
        await db.open_pool("host=127.0.0.1 port=1 connect_timeout=1")
        try:
            async with db.connection():
                pass
        finally:
            await db.close_pool()

    with pytest.raises(psycopg.OperationalError):
        run(scenario())
