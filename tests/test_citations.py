from decimal import Decimal

import pytest

from backend.citations import AliasBook, definitive_order, figures, mask_receipts, unlisted_number


def test_aliases_follow_first_seen_order_and_repeat_for_the_same_receipt():
    book = AliasBook()
    assert [book.alias(r) for r in ("20260101000002", "20260101000001", "20260101000002")] == [
        "DOC-1", "DOC-2", "DOC-1"]
    assert book.doc_map() == {"DOC-1": "20260101000002", "DOC-2": "20260101000001"}
    # A separate book is a separate namespace.
    assert AliasBook().alias("20260101000001") == "DOC-1"


def test_documents_mask_only_standalone_14_digit_receipts():
    book = AliasBook()
    document = book.document({"rcept_no": "20260101000001", "report_nm": "정정 20260101000009 보고",
                              "chunk_text": "번호 20260101000001 와 123456789012345 및 2026010100000"})
    assert document == {"source": "DOC-1", "title": "정정 (공시번호 생략) 보고",
                        "text": "번호 (공시번호 생략) 와 123456789012345 및 2026010100000"}
    assert mask_receipts("없음") == "없음"


@pytest.mark.parametrize("text,order", [
    ("지금 매수하세요.", True), ("삼성전자를 매수하세요.", True), ("꼭 사세요", True),
    ("전량 팔아야 합니다.", True), ("당장 매도하십시오.", True),
    ("반드시 확인 후 매수 검토", False), ("꼭 매수를 검토하세요.", False), ("당장 매도를 고려할 시점은 아닙니다.", False),
    ("변동성이 사라지면 박스권입니다.", False), ("매수하라는 뜻은 아닙니다.", False),
])
def test_definitive_order_matches_imperatives_not_review_conditions(text, order):
    assert definitive_order(text) is order


@pytest.mark.parametrize("text,value", [
    ("101만4000원", 1014000), ("101만 4,000원", 1014000), ("101만4천원", 1014000), ("101만4000", 1014000),
    ("1억2000만원", 120000000), ("1억 2,000만 5,000원", 120005000), ("101만원", 1010000), ("1,014,000원", 1014000),
])
def test_compound_korean_amount_is_one_figure(text, value):
    assert figures(text) == {Decimal(value)}
    assert unlisted_number(f"저점은 {text}입니다.", {Decimal(value)}) is None
    # The compound value itself must be listed; its parts alone do not pass.
    assert unlisted_number(f"저점은 {text}입니다.", {Decimal(1010000), Decimal(4000), Decimal(100000000)}) \
        in (None if value in (1010000,) else text.strip(),)


def test_compound_amount_ignores_sign_and_stops_at_units_years_and_counters():
    assert figures("하락폭 -101만4000원") == {Decimal(1014000)}
    assert figures("101만원 4000원") == {Decimal(1010000), Decimal(4000)}
    assert figures("101만 1,113,800원") == {Decimal(1010000), Decimal(1113800)}
    assert figures("101만 2026년") == {Decimal(1010000)}
    assert figures("101만 10거래일") == {Decimal(1010000)}
    assert figures("101만 2주") == {Decimal(1010000)}


def test_large_plain_numbers_before_counters_stay_price_like():
    assert figures("1,500회 거래") == {Decimal(1500)} and figures("10거래일 흐름") == set()
    assert figures("2026년 실적") == set() and figures("1만주 매수") == {Decimal(10000)}
