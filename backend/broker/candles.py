"""Pure OHLCV parsing and period bucketing for chart candles."""

from datetime import date, timedelta
from decimal import DecimalException

from backend.broker.models import ZERO, Candle, number


def candle_values(row, close_field, volume_field):
    try:
        values = tuple(number(row[field]) for field in (
            "stck_oprc", "stck_hgpr", "stck_lwpr", close_field, volume_field,
        ))
    except (KeyError, ValueError, DecimalException):
        return None
    if min(values[:4]) > ZERO and values[4] >= ZERO:
        return values


def candle_period_start(session: date, period: str) -> date:
    if period == "Y":
        return session.replace(month=1, day=1)
    if period == "W":
        return session - timedelta(days=session.weekday())
    return session


def fold_bar(candles: dict, bucket, open_, high, low, close, volume) -> None:
    """Merge one bar into its bucket's candle. Bars must arrive in time order."""
    candle = candles.get(bucket)
    if candle is None:
        candles[bucket] = Candle(bucket, open_, high, low, close, volume)
    else:
        candle.high = max(candle.high, high)
        candle.low = min(candle.low, low)
        candle.close = close
        candle.volume += volume


def aggregate_ten_minute_candles(rows) -> list[Candle]:
    candles: dict[int, Candle] = {}
    for stamp, values in sorted(rows.items()):
        fold_bar(candles, stamp // 600 * 600, *values)
    return list(candles.values())
