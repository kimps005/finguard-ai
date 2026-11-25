"""DOC-N aliases for DART receipts, receipt masking and the number guard for LLM text."""

from decimal import ROUND_HALF_UP, Decimal
import re


ALIAS = re.compile(r"DOC-[0-9]+")
CITATION_MARK = re.compile(r"\[DOC-[0-9]+\]|" + ALIAS.pattern)
TAG = re.compile(r"(?<![A-Za-z0-9])(?:DOC|NEWS|P)-[0-9]+(?![0-9])")
TAG_MARK = re.compile(r"\[(?:DOC|NEWS|P)-[0-9]+\]")
RECEIPT = re.compile(r"(?<![0-9])[0-9]{14}(?![0-9])")
RECEIPT_PLACEHOLDER = "(공시번호 생략)"
NUMBER = re.compile(r"(?<![0-9.,A-Za-z-])([0-9][0-9,]*(?:\.[0-9]+)?)(?:\s?([천만억]))?(?:\s?(원|주|%))?(년)?")
_PERIOD_SUFFIX = tuple("간일차봉선째")
_MULTIPLIER = {"천": Decimal(1000), "만": Decimal(10000), "억": Decimal(10) ** 8}


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


def with_rounded(values) -> set[Decimal]:
    """Each value and its whole-won rounding, the forms an LLM may legitimately quote."""
    out = set()
    for value in values:
        out.add(value)
        out.add(value.to_integral_value(ROUND_HALF_UP))
    return out


def _figures(text: str):
    """(written form, value) of each price-like figure: a unit, a multiplier or a large plain number."""
    for match in NUMBER.finditer(text):
        raw, mult, unit, year = match.groups()
        digits = raw.replace(",", "")
        if not (unit or mult) and (year or not ("," in raw or len(digits.split(".")[0]) >= 4)):
            continue  # plain small number or a year, not a price-like figure
        if unit == "주" and not mult and (raw == "2" or text[match.end():match.end() + 1] in _PERIOD_SUFFIX):
            continue  # "2주" is the card horizon; "3주간", "10주선" are periods, not share counts
        yield match[0].strip(), Decimal(digits) * _MULTIPLIER.get(mult, Decimal(1))


def figures(text: str) -> set[Decimal]:
    return {value for _, value in _figures(text)}


def unlisted_number(text: str, allowed: set[Decimal]) -> str | None:
    """The first price-like figure whose value is not in `allowed`, as written; None when all are listed."""
    return next((written for written, value in _figures(text) if value not in allowed), None)
