"""Shared PostgreSQL fixtures for tests that need real SQL (pgvector, tsvector, NULLS NOT DISTINCT)."""

from contextlib import ExitStack
import os
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest


MIGRATIONS = Path(__file__).resolve().parents[1] / "backend" / "migrations"


@pytest.fixture(scope="session")
def database(tmp_path_factory):
    """Use an ephemeral PostgreSQL or a supplied test DSN, in a private schema."""
    dsn = os.getenv("MODULE4_TEST_DSN")
    schema = "test_db_" + uuid4().hex
    with ExitStack() as stack:
        try:
            if dsn:
                pg = None
            else:
                from embedded_postgres import get_server

                pg = stack.enter_context(get_server(tmp_path_factory.mktemp("test-pg")))
        except Exception as exc:
            # Windows Smart App Control, for example, blocks the unsigned bundled initdb.exe.
            pytest.skip(f"내장 PostgreSQL을 시작할 수 없습니다 ({type(exc).__name__}). "
                        "pgvector DB를 띄우고 MODULE4_TEST_DSN을 설정하세요.")
        dsn = dsn or pg.get_uri()
        with psycopg.connect(dsn, autocommit=True, prepare_threshold=None) as conn:
            conn.execute(f"CREATE SCHEMA {schema}")
            try:
                conn.execute(f"SET search_path TO {schema}, public")
                # 004 runs twice to prove the migration is idempotent.
                for migration in ["001_module1.sql", "004_module4.sql", "004_module4.sql", "005_stock_market.sql"]:
                    conn.execute((MIGRATIONS / migration).read_text(encoding="utf-8"))
                yield dsn, schema
            finally:
                conn.rollback()
                conn.execute(f"DROP SCHEMA {schema} CASCADE")


@pytest.fixture
def db(database, monkeypatch):
    dsn, schema = database
    connect = psycopg.AsyncConnection.connect

    async def connect_test(**kwargs):
        connection = await connect(dsn, prepare_threshold=None, **kwargs)
        await connection.execute(f"SET search_path TO {schema}, public")
        await connection.commit()
        return connection

    monkeypatch.setattr(psycopg.AsyncConnection, "connect", connect_test)
    with psycopg.connect(dsn, autocommit=True, prepare_threshold=None) as conn:
        conn.execute(f"SET search_path TO {schema}, public")
        conn.execute("TRUNCATE stock_master, dart_chunks, overhang_events")
        yield conn
