"""Score logged price cards against the 10 sessions after as_of and print AI-vs-local hit rates."""

import asyncio
from datetime import date, timedelta
import logging
import os
from pathlib import Path
import sys

from psycopg.rows import dict_row

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend import db
from backend.broker import BrokerConnector
from backend.market_time import KST, confirmed_day
from backend.card_log import HORIZON_SESSIONS, score_window
from backend.toss_broker import TossSettings, toss_account


logger = logging.getLogger("score_price_cards")
# The window grows toward the confirmed day, so a long holiday cluster only delays scoring; capped at this span.
MAX_FETCH_DAYS = 45
# (AI column, local column, the level's source column). A level whose source is 'local' carries the
# local price in its AI column, so AI rates count only rows where that level came from the analyst.
METRICS = (("low_held", "local_low_held", "low_source"), ("high_reached", "local_high_reached", "high_source"),
           ("ceiling_held", "local_ceiling_held", "ceiling_source"))
RESULT_COLUMNS = tuple(column for ai, local, _ in METRICS for column in (ai, local))


async def unscored_rows(confirmed: date) -> list[dict]:
    """Rows without a score whose 10-session window can have closed."""
    async with db.connection(row_factory=dict_row) as conn:
        cursor = await conn.execute(
            "SELECT * FROM price_card_log WHERE scored_at IS NULL AND as_of < %s ORDER BY as_of, stock_code",
            (confirmed,))
        return await cursor.fetchall()


def window_bars(as_of: date, bars, confirmed: date) -> list:
    """The first 10 final sessions strictly after as_of; an in-progress bar is never included."""
    final = sorted((bar for bar in bars if as_of < bar.session <= confirmed), key=lambda bar: bar.session)
    return final[:HORIZON_SESSIONS]


async def fetch_window(broker, row: dict, confirmed: date) -> list:
    start = row["as_of"] + timedelta(days=1)
    end = min(confirmed, row["as_of"] + timedelta(days=MAX_FETCH_DAYS))
    if start > end:
        return []
    bars = await broker.get_daily_bars(row["stock_code"], start, end)
    return window_bars(row["as_of"], bars, confirmed)


async def save_score(row: dict, result: dict) -> None:
    assignments = ", ".join(f"{column} = %({column})s" for column in RESULT_COLUMNS)
    async with db.connection() as conn:
        await conn.execute(f"UPDATE price_card_log SET scored_at = now(), {assignments} "
                           "WHERE stock_code = %(stock_code)s AND as_of = %(as_of)s",
                           {**result, "stock_code": row["stock_code"], "as_of": row["as_of"]})


async def score_pending(broker, confirmed: date) -> int:
    scored = 0
    for row in await unscored_rows(confirmed):
        try:
            result = score_window(row, await fetch_window(broker, row, confirmed))
        except Exception as exc:
            logger.warning("Skipping %s %s (%s)", row["stock_code"], row["as_of"], type(exc).__name__)
            continue
        if result is not None:
            await save_score(row, result)
            scored += 1
    return scored


def hit_rate(rows: list[dict], column: str) -> str:
    values = [row[column] for row in rows if row[column] is not None]
    return f"{sum(values) * 100 / len(values):.1f}% ({sum(values)}/{len(values)})" if values else "-"


async def summary_rows() -> list[dict]:
    async with db.connection(row_factory=dict_row) as conn:
        cursor = await conn.execute("SELECT * FROM price_card_log WHERE scored_at IS NOT NULL")
        return await cursor.fetchall()


def summary_table(rows: list[dict]) -> str:
    """AI rate over AI-sourced levels, local rate over the same rows, and local rate over every row."""
    lines = [f"scored cards: {len(rows)}",
             f"{'metric':<14}{'AI (AI levels)':>20}{'local (same)':>20}{'local (all)':>20}"]
    for ai, local, source in METRICS:
        sourced = [row for row in rows if row.get(source) == "ai"]
        lines.append(f"{ai:<14}{hit_rate(sourced, ai):>20}{hit_rate(sourced, local):>20}"
                     f"{hit_rate(rows, local):>20}")
    return "\n".join(lines)


async def main() -> None:
    await db.open_pool()
    try:
        # Same provider switch as backend.main: Toss by default, KIS when BROKER_PROVIDER=kis.
        provider = os.getenv("BROKER_PROVIDER", "toss").strip().lower()
        if provider not in ("toss", "kis"):
            raise ValueError("BROKER_PROVIDER must be toss or kis")
        account = toss_account(TossSettings.from_env()) if provider == "toss" else None
        async with BrokerConnector.from_env(account=account) as broker:
            scored = await score_pending(broker, confirmed_day(broker.clock().astimezone(KST)))
        print(f"newly scored: {scored}")
        print(summary_table(await summary_rows()))
    finally:
        await db.close_pool()


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    asyncio.run(main())
