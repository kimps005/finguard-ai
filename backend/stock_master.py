"""Read access to stock_master, which backend.sync_stock_master writes."""

import psycopg
from psycopg.rows import dict_row

from backend.sync_stock_master import Stock


async def fetch_stocks(codes: list[str]) -> dict[str, Stock]:
    """Return the canonical rows for these codes; unknown codes are simply absent."""
    if not codes:
        return {}
    async with await psycopg.AsyncConnection.connect(row_factory=dict_row) as conn:
        cursor = await conn.execute(
            "SELECT stock_code, corp_code, isin_cd, crno, corp_name, asset_type, induty_code, "
            "listed_shares::numeric, market FROM stock_master WHERE stock_code = ANY(%s)", (codes,))
        return {row["stock_code"]: Stock(**row) for row in await cursor.fetchall()}


async def search_stocks(query: str) -> list[dict]:
    """Exact code first, then exact, prefix and substring name matches; at most eight."""
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
