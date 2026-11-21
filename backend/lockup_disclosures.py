"""Extract explicit DART lock-up schedules from Module 4's Markdown chunks."""

from datetime import date
from decimal import Decimal
import re
from unicodedata import normalize


_LOCKUP = re.compile(r"보호예수|의무보유(?:등록)?")
_RELEASE = re.compile(r"해제|반환|만료")
_DATE = re.compile(
    r"(?<!\d)(20\d{2})(?:[-./년]\s*(\d{1,2})[-./월]\s*(\d{1,2})일?"
    r"|(\d{2})(\d{2}))(?!\d)"
)
_SHARES = re.compile(
    r"(?<![\d,.])([1-9]\d{0,2}(?:,\d{3})+|[1-9]\d*)(?![\d,.])"
    r"\s*주(?:[이가을를은는의](?=\W|$))?(?![가-힣]|\s*(?:간|후|동안|경과))"
)
_COUNT_CELL = re.compile(r"([1-9]\d{0,2}(?:,\d{3})+|[1-9]\d*)\s*(?:주)?")
_ISIN = re.compile(r"\b[A-Z]{2}[A-Z0-9]{9}[0-9]\b")
_PREFERRED_NAME = re.compile(r"(?:우(?:선주|[A-Z])?|\d+우[A-Z]?)$")


def _release_date(text: str) -> date | None:
    dates = set()
    for year, month, day, compact_month, compact_day in _DATE.findall(text):
        try:
            dates.add(date(int(year), int(month or compact_month), int(day or compact_day)))
        except ValueError:
            return None
    return next(iter(dates)) if len(dates) == 1 else None


def _matches_security(text: str, isin_cd: str, corp_name: str) -> bool:
    codes = set(_ISIN.findall(text))
    if codes:
        return codes == {isin_cd}
    common, preferred = "보통주" in text, "우선주" in text
    if _PREFERRED_NAME.search(corp_name):
        return not common and (corp_name in text or preferred)
    return not preferred


def _date_label(cell: str) -> bool:
    return bool(_RELEASE.search(cell)) and any(word in cell for word in ("일", "예정"))


def _quantity_label(cell: str) -> bool:
    return bool(re.search(r"(?:주식수|수량|물량|주\))", cell))


def disclosed_releases(text: str, isin_cd: str, corp_name: str):
    """Yield date/quantity pairs from an explicit release sentence or table row."""
    text = normalize("NFKC", text)
    lines = text.splitlines()
    if len(lines) >= 2 and lines[0].startswith("|"):
        table = [re.split(r"(?<!\\)\|", line.strip().strip("|")) for line in lines if line.startswith("|")]
        headers = [re.sub(r"\s+", "", cell) for cell in table[0]]
        date_cols = [i for i, cell in enumerate(headers) if _date_label(cell)]
        qty_cols = [i for i, cell in enumerate(headers) if _quantity_label(cell)]
        if (_LOCKUP.search(" ".join(headers)) and len(date_cols) == len(qty_cols) == 1
                and date_cols != qty_cols):
            for row in table[2:]:
                if len(row) != len(headers) or not _matches_security(" ".join(row), isin_cd, corp_name):
                    continue
                released = _release_date(row[date_cols[0]])
                quantity = _COUNT_CELL.fullmatch(row[qty_cols[0]].strip())
                if released and quantity:
                    yield released, Decimal(quantity[1].replace(",", ""))
            return
        fields = [(re.sub(r"\s+", "", label), value.strip())
                  for label, value in (row for row in table if len(row) == 2)]
        dates = [value for label, value in fields if _LOCKUP.search(label) and _date_label(label)]
        counts = [value for label, value in fields if _LOCKUP.search(label) and _quantity_label(label)]
        if len(dates) == len(counts) == 1 and _matches_security(text, isin_cd, corp_name):
            released = _release_date(dates[0])
            quantity = _COUNT_CELL.fullmatch(counts[0])
            if released and quantity:
                yield released, Decimal(quantity[1].replace(",", ""))
                return

    for line in re.split(r"\n|(?<!\d)[.!?]\s+|(?<=[다됨])\.\s+", text):
        compact = re.sub(r"\s+", "", line)
        if not (_LOCKUP.search(compact) and _RELEASE.search(compact)):
            continue
        matches = _SHARES.findall(line)
        released = _release_date(line)
        if released and len(matches) == 1 and _matches_security(line, isin_cd, corp_name):
            yield released, Decimal(matches[0].replace(",", ""))
