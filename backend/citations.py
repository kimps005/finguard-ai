"""DOC-N aliases for DART receipts, receipt masking and the number guard for LLM text."""

from decimal import ROUND_HALF_UP, Decimal
import re


ALIAS = re.compile(r"DOC-[0-9]+")
CITATION_MARK = re.compile(r"\[DOC-[0-9]+\]|" + ALIAS.pattern)
TAG = re.compile(r"(?<![A-Za-z0-9])(?:DOC|NEWS|P)-[0-9]+(?![0-9])")
TAG_MARK = re.compile(r"\[(?:DOC|NEWS|P)-[0-9]+\]")
RECEIPT = re.compile(r"(?<![0-9])[0-9]{14}(?![0-9])")
RECEIPT_PLACEHOLDER = "(공시번호 생략)"
# A leading minus counts as a sign ("-3,900원", "95-3,900원") unless a letter precedes it: P-2, DOC-1, NEWS-3.
NUMBER = re.compile(r"(?:(?<![A-Za-z])(-)|(?<![0-9.,A-Za-z-]))"
                    r"([0-9][0-9,]*(?:\.[0-9]+)?)(?:\s?([천만억]))?(?:\s?(원|주|%))?(년)?")
_PERIOD_SUFFIX = tuple("간일차봉선째")
# A firm buy/sell order, as opposed to a "검토"/"고려" condition: imperative endings ("매수하세요",
# "사세요", "팔아야 합니다") and emphasized verbs ("반드시 … 매수", "꼭 사세요"). "사라지다" is not "사라",
# and "매수하라는 뜻은 아니다" quotes rather than gives an order.
_ORDER_VERB = (r"(?:(?:매수|매도)\s*(?:하세요|하십시오|하시길|(?:해라|하라)(?![는고]))"
               r"|사세요|파세요|사라(?![지져진졌질])|팔아라|(?:사야|팔아야)\s*(?:합니다|해요))")
DEFINITIVE_ORDER = re.compile(
    rf"(?:{_ORDER_VERB}|(?:반드시|무조건|지금\s*바로|당장|꼭)\s*.{{0,6}}?(?:{_ORDER_VERB}|매수|매도|사야|팔아야))"
    r"(?![^.!?\n]{0,6}?(?:검토|고려))")


def definitive_order(text: str) -> bool:
    """True when `text` gives a firm buy/sell order rather than a condition to review."""
    return bool(DEFINITIVE_ORDER.search(text))
# Counter words after a plain number ("10거래일", "3개", "2배", "5위") mark a count, not a price.
_COUNTERS = tuple("일거개배위회번분시초차명건월")
_NEAR_LOW, _NEAR_HIGH = Decimal("0.5"), Decimal("1.5")
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


def _near(value: Decimal, prices) -> bool:
    return any(price * _NEAR_LOW <= value <= price * _NEAR_HIGH for price in prices)


def _large(raw: str) -> bool:
    """A plain number written with a comma or four or more integer digits reads as a price."""
    return "," in raw or len(raw.split(".")[0]) >= 4


def _parts(text: str):
    """(match, absolute value, multiplier) of each NUMBER match, skipping years, counts and period words."""
    for match in NUMBER.finditer(text):
        _, raw, mult, unit, year = match.groups()
        digits = raw.replace(",", "")
        if not (unit or mult) and (year or (not _large(raw) and text[match.end():match.end() + 1] in _COUNTERS)):
            continue  # a year ("2026년") or a small count ("10거래일"), never a price
        if unit == "주" and not mult and (raw == "2" or text[match.end():match.end() + 1] in _PERIOD_SUFFIX):
            continue  # "2주" is the card horizon; "3주간", "10주선" are periods, not share counts
        scale = _MULTIPLIER.get(mult, Decimal(1))
        yield match, Decimal(digits) * scale, scale


def _figures(text: str, near=()):
    """(written form, absolute value) of each price-like figure: a unit, a multiplier or a large plain number.

    A compound Korean amount reads as one figure: "101만4000원" and "101만 4,000원" are 1,014,000 and
    "1억2000만원" is 120,000,000. A part joins the amount before it when it directly follows a unitless
    multiplier and stays below it. A small plain number within ±50% of a price in `near` (a current price)
    is price-like too, so "785 부근" counts for a sub-1,000원 stock.
    """
    parts = list(_parts(text))
    i = 0
    while i < len(parts):
        first, value, scale = parts[i]
        last = first
        while (i + 1 < len(parts) and last[4] is None and scale > 1 and not parts[i + 1][0][1]
               and text[last.end():parts[i + 1][0].start()] in ("", " ") and parts[i + 1][1] < scale):
            i += 1
            last, part, scale = parts[i]
            value += part
        i += 1
        raw, mult, unit = first[2], first[3], last[4]
        if last is first and not (unit or mult) and not _large(raw):
            if not _near(Decimal(raw), near):
                continue  # plain small number, not a price-like figure
        yield text[first.start():last.end()].strip(), value


def figures(text: str) -> set[Decimal]:
    return {value for _, value in _figures(text)}


def unlisted_number(text: str, allowed: set[Decimal], near=()) -> str | None:
    """The first price-like figure not in `allowed`, as written; None when all are listed.

    Figures compare by absolute value, so a listed -16.67 allows "-16.67%" and "16.67% 손실".
    `near` holds current prices that make small plain numbers around them price-like.
    """
    listed = {abs(value) for value in allowed}
    return next((written for written, value in _figures(text, near) if value not in listed), None)
