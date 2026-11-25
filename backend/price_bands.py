"""Local price bands: the single-stock stream delegates to the analyst price card."""

import asyncio
from datetime import timedelta
from decimal import Decimal, ROUND_HALF_UP
import logging

from backend.market_time import KST, confirmed_day


logger = logging.getLogger(__name__)
DISCLOSURE_QUERY = "향후 1개월 주가에 영향을 주는 실적, 주요 계약, 증자, 전환사채, 보호예수 공시"
LEVELS = ("buy", "sell", "ceiling")
TARGET_LEVELS = dict(zip(LEVELS, ("buy_zone", "sell_zone", "peak_limit"), strict=True))


class StockPriceUnavailable(RuntimeError):
    pass


async def optional_data(operation, timeout, *, default=None):
    try:
        async with asyncio.timeout(timeout):
            return await operation
    except Exception as exc:
        logger.warning("Short-term analysis input unavailable (%s)", type(exc).__name__)
        return default


def pct(amount, base):
    if base <= 0:
        return None
    return ((amount - base) * 100 / base).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def calculate_market_band(stock_code, name, current, bars, flow):
    result = {"stock_code": stock_code, "name": name, "current_price": current,
              "levels": None, "disclosures": []}
    if current <= 0:
        return result, None

    window = [bar for bar in bars if 0 < bar.low <= bar.close <= bar.high][-20:]
    count = len(window)
    ma20 = sum((bar.close for bar in window), Decimal(0)) / 20 if count == 20 else None
    low = min((bar.low for bar in window), default=current)
    high = max((bar.high for bar in window), default=current)
    indicative = count < 2 or high == low
    if indicative:
        step = max(Decimal(1), current * Decimal("0.05"))
        support, resistance = current - step, current + 2 * step
        buy_reason = "변동폭 자료가 부족해 현재가에 가정 변동폭을 적용한 하방 참고선입니다."
        ceiling_reason = "현재가 기준 가정 범위의 상단입니다. 도달하면 비중 확대 전에 저항을 다시 확인하세요."
    else:
        step = max(Decimal(1), sum((bar.high - bar.low for bar in window), Decimal(0)) / count)
        use_ma = ma20 is not None and low <= ma20 <= current - step
        support = min(current - step, ma20 if use_ma else low)
        resistance = max(high, current + 2 * step)
        buy_reason = ("20일 이동평균선이 최근 저가 범위 안의 지지 후보입니다." if use_ma else
                      "최근 저가와 평균 일중 변동폭을 반영한 하방 지지 후보입니다.")
        ceiling_reason = "최근 고가와 일중 변동폭을 반영한 저항 후보로, 도달 시 비중 축소를 검토할 구간입니다."

    pressure = flow.foreign_net_buy + flow.institution_net_buy if flow is not None else Decimal(0)
    target_fraction = Decimal("0.75") if pressure > 0 else Decimal("0.5") if pressure < 0 else Decimal("0.67")
    target = current + (resistance - current) * target_fraction
    sell_reason = {
        1: "최근 5거래일 외국인과 기관의 합산 순매수를 반영해 상단 쪽에서 차익 실현을 검토합니다.",
        -1: "외국인과 기관 합산 순매도 부담을 반영해 저항선에 앞서 차익 실현을 검토합니다.",
        0: "현재가와 상단 저항 사이에서 먼저 차익 실현을 검토할 가격입니다.",
    }[int(pressure.compare(0))]
    buy = max(Decimal(1), support.to_integral_value(rounding=ROUND_HALF_UP))
    sell = max(buy + 1, target.to_integral_value(rounding=ROUND_HALF_UP))
    ceiling = max(sell + 1, resistance.to_integral_value(rounding=ROUND_HALF_UP))
    position = max(Decimal(0), min(Decimal(100), (current - buy) * 100 / (sell - buy)))
    result.update(
        levels={"buy": {"price": buy, "reason": buy_reason, "current_pct": pct(buy, current)},
                "sell": {"price": sell, "reason": sell_reason, "current_pct": pct(sell, current)},
                "ceiling": {"price": ceiling, "reason": ceiling_reason, "current_pct": pct(ceiling, current)}},
        position_pct=position.quantize(Decimal("0.01")),
        indicative=indicative,
        ma20=ma20.quantize(Decimal(1), rounding=ROUND_HALF_UP) if ma20 is not None else None,
        recent_range={"sessions": count, "low": low, "high": high} if count else None,
        flow={"foreign": flow.foreign_net_buy, "institution": flow.institution_net_buy} if flow is not None else None,
    )
    signals = {"name": name, "scores": {"ma20": int(current.compare(ma20)) if ma20 is not None else None,
                          "foreign": int(flow.foreign_net_buy.compare(0)) if flow is not None else None,
                          "institution": int(flow.institution_net_buy.compare(0)) if flow is not None else None}}
    return result, signals


def calculate_band(holding, bars, flow):
    result, signals = calculate_market_band(
        holding.stock_code, holding.name, holding.current_price, bars, flow)
    result["average_price"] = holding.average_price
    if signals is not None:
        result["levels"]["sell"]["average_pct"] = pct(result["levels"]["sell"]["price"], holding.average_price)
        signals.update(weight_pct=holding.weight * 100, profit_loss_pct=pct(holding.current_price, holding.average_price))
    return result, signals


async def stock_price_band_updates(market, stock_code):
    """Selected-stock card: a local snapshot first, then the validated analyst card."""
    from backend.price_card import card_updates  # price_card builds on this module

    async for snapshot in card_updates(market, stock_code):
        yield snapshot


async def price_band_updates(market):
    """Every holding's band from local calculation only; no LLM is called in this mode."""
    balance = await market.broker.get_balance()
    now = market.broker.clock().astimezone(KST)
    end = confirmed_day(now)

    async def collect(holding):
        bars, flow = await asyncio.gather(
            optional_data(market.broker.get_daily_bars(holding.stock_code, end - timedelta(days=60), end,
                                                       asset_type=holding.asset_type), 10, default=()),
            optional_data(market.broker.get_five_day_flow(holding.stock_code, as_of=now,
                                                         asset_type=holding.asset_type), 10),
        )
        return calculate_band(holding, bars, flow)

    holdings = sorted(balance.holdings, key=lambda holding: (-holding.weight, holding.stock_code))
    rows = await asyncio.gather(*(collect(holding) for holding in holdings))
    snapshot = {"as_of": now.isoformat(timespec="seconds"), "bands": [band for band, _ in rows]}
    # main.py streams a preliminary and a final frame; both carry the same local bands here.
    yield snapshot
    yield snapshot
