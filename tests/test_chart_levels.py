"""Tests for the deterministic chart indicator engine."""

from datetime import date, timedelta
from decimal import Decimal as D

import pytest

from backend.broker import DailyBar, FiveDayFlow
from backend.chart_levels import (
    average_true_range, chart_facts, moving_average, round_to_tick, swing_points, volume_profile,
)

DAY = date(2026, 11, 20)


def bar(day, low, high, close, volume=10):
    return DailyBar(DAY - timedelta(days=day), D(close), D(high), D(low), D(close), D(volume))


def bars_closing(closes):
    n = len(closes)
    return tuple(bar(n - 1 - i, c, c, c) for i, c in enumerate(closes))


def flat_bars(n, *, low, high, close=100):
    return tuple(bar(n - 1 - i, low, high, close) for i in range(n))


def series_with_low_at(*, day, price, n=30):
    return tuple(
        bar(n - 1 - i, price if n - 1 - i == day else 100, 101, 100) for i in range(n)
    )


def two_cluster_bars():
    low = [bar(9 - i, 99, 101, 100, 100) for i in range(3)]
    high = [bar(6 - i, 199, 201, 200, 50) for i in range(2)]
    return tuple(low + high)


def test_moving_average_needs_full_window():
    assert moving_average(bars_closing([1, 2, 3]), 3) == D(2)
    assert moving_average(bars_closing([1, 2]), 3) is None


def test_atr_uses_previous_close_and_needs_window_plus_one():
    assert average_true_range(flat_bars(15, low=99, high=101)) == D(2)
    assert average_true_range(flat_bars(14, low=99, high=101)) is None


def test_swing_points_require_strict_extremes_on_both_sides():
    lows, highs = swing_points(series_with_low_at(day=10, price=D(90)), width=2)
    assert [price for _, price in lows] == [D(90)]


def test_volume_profile_ranks_bins_by_volume():
    top = volume_profile(two_cluster_bars(), bins=10, top=1)
    assert top[0][2] == D(300)


@pytest.mark.parametrize("price,asset,expected", [
    (D("1999.4"), "STOCK", D(1999)), (D("3102"), "STOCK", D(3100)), (D("3103"), "STOCK", D(3105)),
    (D("19996"), "STOCK", D(20000)), (D("49970"), "STOCK", D(49950)), (D("3102"), "ETF", D(3100)),
])
def test_round_to_tick(price, asset, expected):
    assert round_to_tick(price, asset) == expected


def test_chart_facts_numbering_skips_missing_items_for_new_listing():
    facts = chart_facts(flat_bars(10, low=99, high=101), D(100), None)
    assert [f.id for f in facts] == [f"P-{i}" for i in range(1, len(facts) + 1)]
    assert {f.label for f in facts} >= {"현재가", "5일선"}
    assert not any(f.label in ("60일선", "ATR14") for f in facts)


def test_chart_facts_full_series_orders_items_and_includes_flow():
    series = series_with_low_at(day=10, price=D(90), n=70)
    flow = FiveDayFlow((), D(5), D(-3))
    facts = chart_facts(series, D(100), flow)
    labels = [f.label for f in facts]
    assert labels[:4] == ["현재가", "5일선", "20일선", "60일선"]
    assert "스윙 저점" in labels and "ATR14" in labels
    assert labels[-2:] == ["외국인 5일 순매수", "기관 5일 순매수"]
    assert [f.id for f in facts] == [f"P-{i}" for i in range(1, len(facts) + 1)]
    zone = next(f for f in facts if f.label == "매물대")
    assert zone.upper is not None and zone.upper >= zone.value
