import asyncio
from datetime import date, datetime, timezone
from decimal import Decimal as D
from unittest.mock import AsyncMock

from backend.broker import BrokerConnector, BrokerError, Candle, DailyBar, KST
from backend.broker.candles import aggregate_ten_minute_candles, candle_period_start, candle_values


def test_period_start_for_daily_weekly_and_yearly_candles():
    thursday = date(2026, 11, 19)
    assert candle_period_start(thursday, "D") == thursday
    assert candle_period_start(thursday, "W") == date(2026, 11, 16)
    assert candle_period_start(thursday, "Y") == date(2026, 1, 1)


def test_minute_rows_fold_into_ten_minute_buckets_in_time_order():
    base = int(datetime(2026, 11, 20, 9, 0, tzinfo=KST).timestamp())
    rows = {  # Inserted out of order; aggregation must sort by time.
        base + 540: (D(12), D(15), D(11), D(14), D(2)),
        base: (D(10), D(12), D(9), D(11), D(1)),
        base + 600: (D(14), D(14), D(13), D(13), D(5)),
    }
    assert aggregate_ten_minute_candles(rows) == [
        Candle(base, D(10), D(15), D(9), D(14), D(3)),
        Candle(base + 600, D(14), D(14), D(13), D(13), D(5)),
    ]


def test_kis_rows_with_missing_or_non_positive_prices_are_skipped():
    row = {"stck_oprc": "1,000", "stck_hgpr": "1100", "stck_lwpr": "900", "stck_clpr": "1050", "acml_vol": "0"}
    assert candle_values(row, "stck_clpr", "acml_vol") == (D(1000), D(1100), D(900), D(1050), D(0))
    assert candle_values(row | {"stck_lwpr": "0"}, "stck_clpr", "acml_vol") is None
    assert candle_values(row | {"acml_vol": "-1"}, "stck_clpr", "acml_vol") is None
    assert candle_values({"stck_oprc": "1"}, "stck_clpr", "acml_vol") is None


class DailyOnly:
    def __init__(self, bars):
        self.bars = bars
        self.calls = []

    def daily(self, code, start, end, asset_type=None, *, adjusted=False):
        self.calls.append((code, start, end, asset_type, adjusted))
        return self.bars


def test_weekly_history_falls_back_to_pykrx_and_folds_daily_bars():
    bars = (
        DailyBar(date(2026, 11, 12), D(9), D(12), D(8), D(11), D(1)),   # previous week
        DailyBar(date(2026, 11, 16), D(10), D(13), D(9), D(12), D(2)),  # Monday
        DailyBar(date(2026, 11, 18), D(12), D(15), D(7), D(14), D(3)),
        DailyBar(date(2026, 11, 17), D(11), D(12), D(10), D(11), D(4)),  # out of order
        DailyBar(date(2026, 11, 19), D(0), D(1), D(0), D(1), D(9)),      # invalid prices
    )
    source = DailyOnly(bars)
    broker = BrokerConnector(None, None, None, market_data=source, sleep=AsyncMock(),
                             clock=lambda: datetime(2026, 11, 20, 1, tzinfo=timezone.utc))
    broker._market = AsyncMock(side_effect=BrokerError("KIS down"))
    candles = asyncio.run(broker.get_candle_history("005930", "W"))
    assert candles == [
        Candle(date(2026, 11, 9), D(9), D(12), D(8), D(11), D(1)),
        Candle(date(2026, 11, 16), D(10), D(15), D(7), D(14), D(9)),
    ]
    assert source.calls == [("005930", date(2021, 11, 21), date(2026, 11, 20), None, False)]
