from decimal import Decimal as D

import pytest

from backend.card_validation import (
    CardAnalysis, LevelProposal, Materials, Risk, parse_price, validate_card,
)
from backend.chart_levels import Fact
from backend.citations import TagBook

FACTS = [
    Fact("P-1", "현재가", D(3100)),
    Fact("P-2", "스윙 저점", D(2980)),
    Fact("P-3", "20일선", D("3168.4")),
    Fact("P-4", "매물대", D(3250), D(3300)),
]
NEWS = {"NEWS-1": {"alias": "NEWS-1", "title": "악재 기사", "link": "https://example.com/n1"}}
DOCS = {"DOC-1": {"alias": "DOC-1", "title": "분기보고서", "receipt": "20260101000001"}}
M = Materials(facts=FACTS, news=NEWS, docs=DOCS, current=D(3100), atr=D(60), asset_type="STOCK",
              extra_numbers={D(3200)})
LOCAL = {
    "low": {"price": D(2950), "reason": "최근 저점 기준 기본 계산"},
    "high": {"price": D(3200), "reason": "20일선 기준 기본 계산"},
    "ceiling": {"price": D(3350), "reason": "매물대 기준 기본 계산"},
}
BASE = {
    "low": ("2982", ["P-2"], "지지 후보"),
    "high": ("3170", ["P-3"], "저항 후보"),
    "ceiling": ("3290", ["P-4"], "매물대 상단"),
}


def proposal(summary="", risks=None, **levels):
    merged = {}
    for name, default in BASE.items():
        spec = levels.get(name, default)
        if len(spec) == 2:
            spec = (*spec, default[2])
        merged[name] = LevelProposal(price=spec[0], basis=spec[1], reason=spec[2])
    return CardAnalysis(**merged, summary=summary, risks=risks or [], citations=[])


def sources(card):
    return {k: v["source"] for k, v in card["levels"].items()}


def test_valid_proposal_is_accepted_and_tick_rounded():
    card = validate_card(proposal(low=("2982", ["P-2"]), high=("3170", ["P-3"]), ceiling=("3290", ["P-4"])), M, LOCAL)
    assert {k: (v["price"], v["source"]) for k, v in card["levels"].items()} == {
        "low": (D(2980), "ai"), "high": (D(3170), "ai"), "ceiling": (D(3290), "ai")}
    assert card["warnings"] == []


@pytest.mark.parametrize("raw,ok", [("3,100원", True), ("3100.5", False), ("-3100", False), ("약 3천원", False)])
def test_price_formats(raw, ok):
    # low == current 3100 is valid (low <= current, low < high)
    card = validate_card(proposal(low=(raw, ["P-1"])), M, LOCAL)
    assert (card["levels"]["low"]["source"] == "ai") is ok
    if ok:
        assert card["levels"]["low"]["price"] == D(3100)


def test_parse_price():
    assert parse_price("3,100원") == D(3100)
    assert parse_price(" 3100 ") == D(3100)
    assert parse_price("0") is None
    assert parse_price("3100.5") is None


def test_far_from_anchor_needs_news_or_doc():
    assert sources(validate_card(proposal(low=("2880", ["P-2"], "지지 후보")), M, LOCAL))["low"] == "local"
    assert sources(validate_card(proposal(low=("2880", ["P-2"], "[NEWS-1] 악재 반영")), M, LOCAL))["low"] == "ai"
    assert sources(validate_card(proposal(low=("2880", ["P-2"], "DOC-1 공시 반영")), M, LOCAL))["low"] == "ai"


def test_deviation_over_two_atr_is_rejected_even_with_news():
    card = validate_card(proposal(high=("3300", ["P-3"], "[NEWS-1] 호재")), M, LOCAL)
    assert card["levels"]["high"]["source"] == "local"
    assert card["levels"]["high"]["price"] == D(3200)


def test_unknown_basis_only_is_rejected():
    assert sources(validate_card(proposal(low=("2982", ["P-9"])), M, LOCAL))["low"] == "local"
    assert sources(validate_card(proposal(low=("2982", ["NEWS-1"])), M, LOCAL))["low"] == "local"


def test_unknown_tag_or_unlisted_number_in_reason_rejects_level():
    for reason in ("[NEWS-9] 호재", "[P-9] 근거", "공시 20260101000001 참고", "지지선 2,500원 근처",
                   "수익률 7% 기대"):
        card = validate_card(proposal(low=("2982", ["P-2"], reason)), M, LOCAL)
        assert card["levels"]["low"]["source"] == "local", reason
    ok = validate_card(proposal(low=("2982", ["P-2"], "스윙 저점 2,980원과 [P-1] 3100원 사이")), M, LOCAL)
    assert ok["levels"]["low"]["source"] == "ai"
    # rounded integer of an unquantized fact value is allowed
    ok = validate_card(proposal(high=("3170", ["P-3"], "20일선 3168원 부근")), M, LOCAL)
    assert ok["levels"]["high"]["source"] == "ai"
    # extra_numbers allowed
    ok = validate_card(proposal(high=("3170", ["P-3"], "평단 3,200원 위")), M, LOCAL)
    assert ok["levels"]["high"]["source"] == "ai"


def test_missing_atr_uses_local_for_all_levels():
    m = Materials(facts=FACTS, news=NEWS, docs=DOCS, current=D(3100), atr=None, asset_type="STOCK",
                  extra_numbers=set())
    card = validate_card(proposal(), m, LOCAL)
    assert set(sources(card).values()) == {"local"}
    assert card["warnings"].count("변동성 자료 부족으로 기본 계산을 사용했습니다.") == 1
    assert len(card["warnings"]) == 1


def test_mixed_ai_and_local_order_break_falls_back_entirely():
    # high rejected (unknown tag) -> local 3200 exceeds accepted AI ceiling 3170
    card = validate_card(proposal(high=("3170", ["P-3"], "[NEWS-9] 호재"), ceiling=("3170", ["P-3"])), M, LOCAL)
    assert set(sources(card).values()) == {"local"}
    assert {k: v["price"] for k, v in card["levels"].items()} == {
        "low": D(2950), "high": D(3200), "ceiling": D(3350)}
    assert any("순서" in w for w in card["warnings"])


def test_ai_levels_breaking_order_fall_back_entirely():
    card = validate_card(proposal(low=("3170", ["P-3"])), M, LOCAL)  # low above current
    assert set(sources(card).values()) == {"local"}


def test_rejected_level_adds_named_warning():
    card = validate_card(proposal(low=("2982", ["P-2"], "[NEWS-9] 호재")), M, LOCAL)
    assert card["levels"]["low"]["source"] == "local"
    assert card["warnings"] == ["AI 단기 저점 검증 실패로 기본 계산을 사용했습니다."]


def test_llm_failure_returns_local():
    card = validate_card(None, M, LOCAL)
    assert set(sources(card).values()) == {"local"}
    assert card["summary"] == "" and card["risks"] == [] and len(card["warnings"]) == 1


def test_summary_drops_only_bad_sentences_and_collects_cited_sources():
    summary = ("단기 지지선이 유효합니다 [NEWS-1]. 목표가는 9,999원입니다. "
               "공시에서 변동은 제한적입니다 [DOC-1]. 근거 없는 [NEWS-7] 주장입니다.")
    risks = [
        Risk(text="실적 부진 우려 [DOC-1]", basis=["DOC-1"]),
        Risk(text="가짜 호재 [NEWS-5]", basis=["NEWS-5"]),
        Risk(text="기본 위험", basis=["NEWS-8"]),
    ]
    card = validate_card(proposal(summary=summary, risks=risks), M, LOCAL)
    assert "9,999" not in card["summary"] and "NEWS-7" not in card["summary"]
    assert "[NEWS-1]" in card["summary"] and "[DOC-1]" in card["summary"]
    assert [r["text"] for r in card["risks"]] == ["실적 부진 우려 [DOC-1]"]
    assert [n["alias"] for n in card["news"]] == ["NEWS-1"]
    assert card["news"][0] == {"alias": "NEWS-1", "title": "악재 기사", "link": "https://example.com/n1"}
    assert card["docs"] == [{"alias": "DOC-1", "title": "분기보고서", "receipt": "20260101000001"}]


def test_unused_sources_are_not_listed():
    card = validate_card(proposal(summary="지지선 부근입니다."), M, LOCAL)
    assert card["news"] == [] and card["docs"] == []


def test_zone_fact_distance_is_zero_inside_interval():
    card = validate_card(proposal(ceiling=("3290", ["P-4"], "매물대 안")), M, LOCAL)
    assert card["levels"]["ceiling"]["source"] == "ai"


def test_tagbook_renumbers_per_kind_across_cards():
    book = TagBook()
    assert book.rewrite("[NEWS-1] 와 [DOC-1]", {"NEWS-1": book.alias("NEWS", "a"), "DOC-1": book.alias("DOC", "r1")}) == "[NEWS-1] 와 [DOC-1]"
    assert book.alias("NEWS", "b") == "NEWS-2"
    assert book.alias("NEWS", "a") == "NEWS-1"
    assert book.rewrite("[NEWS-1] x [P-3]", {"NEWS-1": "NEWS-2"}) == "[NEWS-2] x [P-3]"
