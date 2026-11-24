"""DOC-N aliases for DART receipts and receipt masking before text reaches a cloud LLM."""

import re


ALIAS = re.compile(r"DOC-[0-9]+")
CITATION_MARK = re.compile(r"\[DOC-[0-9]+\]|" + ALIAS.pattern)
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
