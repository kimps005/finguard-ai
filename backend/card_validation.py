"""Validate AI-proposed 2-week price levels against numbered materials; replace failures locally."""

import re
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal

from pydantic import BaseModel, Field

from backend.chart_levels import Fact, round_to_tick
from backend.citations import RECEIPT, TAG
from backend.stock_analysis import cited_sentences

LEVELS = ("low", "high", "ceiling")
LEVEL_NAMES = {"low": "단기 저점", "high": "단기 고점", "ceiling": "단기 천장"}
ATR_WARNING = "변동성 자료 부족으로 기본 계산을 사용했습니다."
_UNIT_NUMBER = re.compile(r"(?<![0-9.,])([0-9][0-9,]*(?:\.[0-9]+)?)\s?(?:원|주|%)")


class LevelProposal(BaseModel):
    price: str
    basis: list[str]
    reason: str = Field(max_length=200)


class Risk(BaseModel):
    text: str = Field(max_length=120)
    basis: list[str]


class CardAnalysis(BaseModel):
    low: LevelProposal
    high: LevelProposal
    ceiling: LevelProposal
    summary: str = Field(max_length=400)
    risks: list[Risk] = Field(max_length=3)
    citations: list[str]


@dataclass
class Materials:
    facts: list[Fact]
    news: dict[str, dict]
    docs: dict[str, dict]
    current: Decimal
    atr: Decimal | None
    asset_type: str = "STOCK"
    extra_numbers: set[Decimal] = field(default_factory=set)


def parse_price(text: str) -> Decimal | None:
    cleaned = re.sub(r"\s+", "", text.replace(",", ""))
    if cleaned.endswith("원"):
        cleaned = cleaned[:-1]
    return Decimal(cleaned) if re.fullmatch(r"[1-9][0-9]*", cleaned) else None


def _with_rounded(values) -> set[Decimal]:
    out = set()
    for value in values:
        out.add(value)
        out.add(value.to_integral_value(ROUND_HALF_UP))
    return out


def _known_tags(m: Materials) -> set[str]:
    return {f.id for f in m.facts} | set(m.news) | set(m.docs)


def _text_ok(text: str, m: Materials, allowed: set[Decimal]) -> bool:
    if RECEIPT.search(text) or any(tag not in _known_tags(m) for tag in TAG.findall(text)):
        return False
    return all(Decimal(n.replace(",", "")) in allowed for n in _UNIT_NUMBER.findall(text))


def _distance(price: Decimal, fact: Fact) -> Decimal:
    ends = (fact.value, fact.upper if fact.upper is not None else fact.value)
    return max(min(ends) - price, price - max(ends), Decimal(0))


def _level_ok(p: LevelProposal, m: Materials, allowed: set[Decimal]) -> Decimal | None:
    parsed = parse_price(p.price)
    if parsed is None:
        return None
    price = round_to_tick(parsed, m.asset_type)
    atr = m.atr
    if abs(price - m.current) > 4 * atr:
        return None
    facts = {f.id: f for f in m.facts}
    anchors = [facts[t] for t in p.basis if t.startswith("P-") and t in facts]
    if not anchors:
        return None
    gap = min(_distance(price, f) for f in anchors)
    if gap > 2 * atr:
        return None
    if gap > max(atr, m.current * Decimal("0.03")):
        known = _known_tags(m)
        if not any(t.startswith(("NEWS-", "DOC-")) and t in known for t in TAG.findall(p.reason)):
            return None
    if not _text_ok(p.reason, m, allowed | _with_rounded({parsed, price})):
        return None
    return price


def _order_ok(prices: dict[str, Decimal], current: Decimal) -> bool:
    return (prices["low"] <= current <= prices["ceiling"]
            and prices["low"] < prices["high"] <= prices["ceiling"])


def validate_card(analysis: CardAnalysis | None, materials: Materials, local: dict[str, dict]) -> dict:
    m = materials
    result = {"levels": {}, "summary": "", "risks": [], "news": [], "docs": [], "warnings": []}

    def use_local(name):
        result["levels"][name] = {"price": local[name]["price"], "reason": local[name]["reason"],
                                  "basis": [], "source": "local"}

    if analysis is None:
        for name in LEVELS:
            use_local(name)
        result["warnings"].append("AI 분석을 사용할 수 없어 기본 계산을 사용했습니다.")
        return result
    if m.atr is None:
        for name in LEVELS:
            use_local(name)
        result["warnings"].append(ATR_WARNING)
        return result

    base_allowed = _with_rounded([v for f in m.facts for v in (f.value, f.upper) if v is not None])
    base_allowed |= _with_rounded(m.extra_numbers)
    known = _known_tags(m)
    cited: set[str] = set()
    for name in LEVELS:
        p = getattr(analysis, name)
        price = _level_ok(p, m, base_allowed)
        if price is None:
            use_local(name)
            result["warnings"].append(f"AI {LEVEL_NAMES[name]} 검증 실패로 기본 계산을 사용했습니다.")
        else:
            result["levels"][name] = {"price": price, "reason": p.reason, "basis": list(p.basis), "source": "ai"}
            cited |= set(TAG.findall(p.reason)) | {t for t in p.basis if t in known}

    if not _order_ok({n: result["levels"][n]["price"] for n in LEVELS}, m.current):
        for name in LEVELS:
            use_local(name)
        result["warnings"].append("AI 가격 순서 검증 실패로 기본 계산을 사용했습니다.")
        cited = set()

    allowed = base_allowed | _with_rounded({v["price"] for v in result["levels"].values()})
    kept = [s for s in cited_sentences(analysis.summary) if _text_ok(s, m, allowed)]
    result["summary"] = " ".join(kept)
    cited |= set(TAG.findall(result["summary"]))
    for risk in analysis.risks:
        if _text_ok(risk.text, m, allowed) and all(t in known for t in risk.basis):
            result["risks"].append({"text": risk.text, "basis": list(risk.basis)})
            cited |= set(TAG.findall(risk.text)) | set(risk.basis)

    def order(alias):
        return int(alias.rsplit("-", 1)[1])

    result["news"] = [{"alias": a, "title": row["title"], "link": row["link"]}
                      for a, row in sorted(m.news.items(), key=lambda kv: order(kv[0])) if a in cited]
    result["docs"] = [{"alias": a, "title": row["title"], "receipt": row["receipt"]}
                      for a, row in sorted(m.docs.items(), key=lambda kv: order(kv[0])) if a in cited]
    return result
