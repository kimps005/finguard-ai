"""Log each freshly built price card so AI and local levels can be scored against the next 10 sessions."""

from collections.abc import Sequence
from datetime import date
from decimal import Decimal

from backend import db
from backend.broker import DailyBar


HORIZON_SESSIONS = 10
LOW_TOLERANCE = Decimal("0.99")
# card["levels"] key -> local key and column prefix.
LEVELS = (("buy_zone", "low"), ("sell_zone", "high"), ("peak_limit", "ceiling"))

UPSERT = """
INSERT INTO price_card_log (stock_code, as_of, current_price, low, high, ceiling,
                            local_low, local_high, local_ceiling, atr14,
                            low_source, high_source, ceiling_source, model)
VALUES (%(stock_code)s, %(as_of)s, %(current_price)s, %(low)s, %(high)s, %(ceiling)s,
        %(local_low)s, %(local_high)s, %(local_ceiling)s, %(atr14)s,
        %(low_source)s, %(high_source)s, %(ceiling_source)s, %(model)s)
ON CONFLICT (stock_code, as_of) DO UPDATE SET
    current_price = EXCLUDED.current_price, low = EXCLUDED.low, high = EXCLUDED.high,
    ceiling = EXCLUDED.ceiling, local_low = EXCLUDED.local_low, local_high = EXCLUDED.local_high,
    local_ceiling = EXCLUDED.local_ceiling, atr14 = EXCLUDED.atr14, low_source = EXCLUDED.low_source,
    high_source = EXCLUDED.high_source, ceiling_source = EXCLUDED.ceiling_source, model = EXCLUDED.model,
    created_at = now(), scored_at = NULL, low_held = NULL, high_reached = NULL, ceiling_held = NULL,
    local_low_held = NULL, local_high_reached = NULL, local_ceiling_held = NULL
"""


async def record_card(stock_code: str, as_of: date, card: dict, local: dict, atr: Decimal | None,
                      model: str) -> None:
    """Upsert one card: `card` is the final snapshot band, `local` maps low/high/ceiling to {"price"}."""
    values = {"stock_code": stock_code, "as_of": as_of, "current_price": card["current_price"],
              "atr14": atr, "model": model}
    for level, name in LEVELS:
        values[name] = card["levels"][level]["price"]
        values[f"{name}_source"] = card["levels"][level]["source"]
        values[f"local_{name}"] = local[name]["price"]
    async with db.connection() as conn:
        await conn.execute(UPSERT, values)


def score_window(row: dict, bars: Sequence[DailyBar]) -> dict | None:
    """Score a logged card against the 10 sessions after as_of; None until all 10 exist."""
    if len(bars) < HORIZON_SESSIONS:
        return None
    lowest, highest = min(bar.low for bar in bars), max(bar.high for bar in bars)
    result = {}
    for prefix, held, reached, capped in (("", "low_held", "high_reached", "ceiling_held"),
                                          ("local_", "local_low_held", "local_high_reached",
                                           "local_ceiling_held")):
        result[held] = lowest >= row[f"{prefix}low"] * LOW_TOLERANCE
        result[reached] = highest >= row[f"{prefix}high"]
        result[capped] = highest <= row[f"{prefix}ceiling"]
    return result
