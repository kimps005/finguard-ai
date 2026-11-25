"""Deterministic chart indicators that feed numbered [P-n] facts to the price card."""

from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal


@dataclass(frozen=True)
class Fact:
    id: str
    label: str
    value: Decimal
    upper: Decimal | None = None
    session: date | None = None


def _ordered(bars):
    return sorted(bars, key=lambda b: b.session)


def moving_average(bars, window: int) -> Decimal | None:
    ordered = _ordered(bars)
    if window <= 0 or len(ordered) < window:
        return None
    return sum((b.close for b in ordered[-window:]), Decimal(0)) / Decimal(window)


def average_true_range(bars, window: int = 14) -> Decimal | None:
    ordered = _ordered(bars)
    if len(ordered) < window + 1:
        return None
    recent = ordered[-(window + 1):]
    ranges = [
        max(cur.high - cur.low, abs(cur.high - prev.close), abs(cur.low - prev.close))
        for prev, cur in zip(recent, recent[1:])
    ]
    return sum(ranges, Decimal(0)) / Decimal(window)


def swing_points(bars, *, lookback: int = 60, width: int = 2):
    window = _ordered(bars)[-lookback:]
    lows: list[tuple[date, Decimal]] = []
    highs: list[tuple[date, Decimal]] = []
    for i in range(width, len(window) - width):
        neighbors = window[i - width:i] + window[i + 1:i + width + 1]
        if all(window[i].low < n.low for n in neighbors):
            lows.append((window[i].session, window[i].low))
        if all(window[i].high > n.high for n in neighbors):
            highs.append((window[i].session, window[i].high))
    return lows, highs


def volume_profile(bars, *, lookback: int = 60, bins: int = 10, top: int = 3):
    window = _ordered(bars)[-lookback:]
    if not window:
        return []
    lo = min(b.low for b in window)
    hi = max(b.high for b in window)
    if hi == lo:
        return [(lo, hi, sum((b.volume for b in window), Decimal(0)))][:top]
    step = (hi - lo) / Decimal(bins)
    totals = [Decimal(0)] * bins
    for b in window:
        index = min(int((b.close - lo) / step), bins - 1)
        totals[index] += b.volume
    ranked = sorted(
        ((lo + step * i, lo + step * (i + 1), total) for i, total in enumerate(totals) if total > 0),
        key=lambda row: row[2],
        reverse=True,
    )
    return ranked[:top]


_TICKS = (
    (Decimal(2000), Decimal(1)), (Decimal(5000), Decimal(5)), (Decimal(20000), Decimal(10)),
    (Decimal(50000), Decimal(50)), (Decimal(200000), Decimal(100)), (Decimal(500000), Decimal(500)),
)


def round_to_tick(price: Decimal, asset_type: str = "STOCK") -> Decimal:
    if asset_type in ("ETF", "ETN"):
        tick = Decimal(5)
    else:
        tick = next((t for limit, t in _TICKS if price < limit), Decimal(1000))
    return (price / tick).to_integral_value(ROUND_HALF_UP) * tick


def chart_facts(bars, current: Decimal, flow) -> list[Fact]:
    drafts: list[tuple[str, Decimal, Decimal | None, date | None]] = [("현재가", current, None, None)]
    for window, label in ((5, "5일선"), (20, "20일선"), (60, "60일선")):
        average = moving_average(bars, window)
        if average is not None:
            drafts.append((label, average, None, None))
    lows, highs = swing_points(bars)
    below = sorted((p for p in lows if p[1] < current), key=lambda p: current - p[1])[:3]
    above = sorted((p for p in highs if p[1] > current), key=lambda p: p[1] - current)[:3]
    drafts += [("스윙 저점", price, None, day) for day, price in below]
    drafts += [("스윙 고점", price, None, day) for day, price in above]
    drafts += [("매물대", low, high, None) for low, high, _ in volume_profile(bars)]
    atr = average_true_range(bars)
    if atr is not None:
        drafts.append(("ATR14", atr, None, None))
    if flow is not None:
        drafts.append(("외국인 5일 순매수", flow.foreign_net_buy, None, None))
        drafts.append(("기관 5일 순매수", flow.institution_net_buy, None, None))
    return [Fact(f"P-{i}", label, value, upper, session) for i, (label, value, upper, session) in enumerate(drafts, 1)]
