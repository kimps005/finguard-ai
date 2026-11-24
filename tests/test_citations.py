from backend.citations import AliasBook, mask_receipts


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
