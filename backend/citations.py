"""DOC-N aliases for DART receipts and receipt masking before text reaches a cloud LLM."""

import re


ALIAS = re.compile(r"DOC-[0-9]+")
CITATION_MARK = re.compile(r"\[DOC-[0-9]+\]|" + ALIAS.pattern)
TAG = re.compile(r"\b(?:DOC|NEWS|P)-[1-9][0-9]*\b")
TAG_MARK = re.compile(r"\[(?:DOC|NEWS|P)-[1-9][0-9]*\]")
RECEIPT = re.compile(r"(?<![0-9])[0-9]{14}(?![0-9])")
RECEIPT_PLACEHOLDER = "(공시번호 생략)"


def mask_receipts(text: str) -> str:
    """Hide 14-digit DART receipt numbers so an LLM can cite filings only by alias."""
    return RECEIPT.sub(RECEIPT_PLACEHOLDER, text)


class AliasBook:
    """Number receipts DOC-1, DOC-2, ... in first-seen order within one citation namespace.

    Each prompt needs its own book: a worker's DOC-1 and the synthesizer's DOC-1 may
    name different filings, and the guardrail resolves aliases against one book only.
    """

    def __init__(self):
        self._aliases: dict[str, str] = {}

    def alias(self, receipt: str) -> str:
        return self._aliases.setdefault(receipt, f"DOC-{len(self._aliases) + 1}")

    def doc_map(self) -> dict[str, str]:
        """Map each alias back to its receipt, in alias order."""
        return {alias: receipt for receipt, alias in self._aliases.items()}

    def document(self, row: dict) -> dict:
        """Turn a retrieved chunk row into the masked document a prompt may carry."""
        return {"source": self.alias(row["rcept_no"]), "title": mask_receipts(row["report_nm"]),
                "text": mask_receipts(row["chunk_text"])}


class TagBook:
    """Global per-kind numbering (NEWS-1, DOC-1, ...) shared by several cards."""

    def __init__(self):
        self._tags: dict[tuple[str, str], str] = {}
        self._counts: dict[str, int] = {}

    def alias(self, kind: str, key: str) -> str:
        if (kind, key) not in self._tags:
            self._counts[kind] = self._counts.get(kind, 0) + 1
            self._tags[(kind, key)] = f"{kind}-{self._counts[kind]}"
        return self._tags[(kind, key)]

    def rewrite(self, text: str, mapping: dict[str, str]) -> str:
        """Replace card-local tags with their global tags in a single pass."""
        return TAG.sub(lambda m: mapping.get(m[0], m[0]), text)
