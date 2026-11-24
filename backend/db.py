"""Async PostgreSQL access through one process-wide pool.

The FastAPI lifespan opens the pool. Code that runs without it (CLI scripts, tests)
falls back to one direct libpq connection per block. Both paths commit when the block
succeeds and roll back when it raises, so callers keep the same transaction semantics.
"""

from contextlib import asynccontextmanager

import psycopg
from psycopg.rows import tuple_row
from psycopg_pool import AsyncConnectionPool


# Requests wait this long for a free connection before PoolTimeout, a psycopg.OperationalError.
ACQUIRE_TIMEOUT = 5.0
_pool: AsyncConnectionPool | None = None


async def open_pool(conninfo: str = "", *, min_size: int = 1, max_size: int = 8) -> AsyncConnectionPool:
    """Open the pool (libpq environment variables by default) without waiting for the database."""
    global _pool
    if _pool is None:
        pool = AsyncConnectionPool(conninfo, min_size=min_size, max_size=max_size, timeout=ACQUIRE_TIMEOUT,
                                   open=False, name="finguard")
        await pool.open(wait=False)
        _pool = pool
    return _pool


async def close_pool() -> None:
    global _pool
    pool, _pool = _pool, None
    if pool is not None:
        await pool.close()


@asynccontextmanager
async def connection(*, row_factory=None):
    """Yield a connection in a transaction; set row_factory for this block only."""
    if _pool is None:
        async with await psycopg.AsyncConnection.connect() as conn:
            if row_factory is not None:
                conn.row_factory = row_factory
            yield conn
        return
    async with _pool.connection() as conn:
        # Pooled connections are reused, so every checkout sets the factory it needs.
        conn.row_factory = row_factory or tuple_row
        yield conn
