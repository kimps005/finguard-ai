"""Convert DART HTML and its HTML-like XML vocabulary into retrievable chunks."""

from collections.abc import Iterator
from bs4 import BeautifulSoup, Comment, Declaration, NavigableString, ProcessingInstruction, Tag


def _line(cells: list[str]) -> str:
    return "| " + " | ".join(cells) + " |"


def _table_rows(table: Tag) -> tuple[list[str], list[list[str]]]:
    rows = []
    headers = 0
    spans = {}
    for tr in table.find_all("tr"):
        if tr.find_parent("table") is not table:
            continue
        cells = tr.find_all(["th", "td", "te", "tu"], recursive=False)
        row = {col: value for col, (_, value) in spans.items()}
        spans = {col: (left - 1, value) for col, (left, value) in spans.items() if left > 1}
        col = 0
        for cell in cells:
            while col in row:
                col += 1
            value = " ".join(" ".join(s for s in cell.find_all(string=True)
                                      if not isinstance(s, Comment)
                                      and s.find_parent("table") is table).split())
            width = int(cell.get("colspan", 1))
            height = int(cell.get("rowspan", 1))
            for offset in range(width):
                row[col + offset] = value
                if height > 1:
                    spans[col + offset] = (height - 1, value)
            col += width
        if not row:
            continue
        if headers == len(rows) and (tr.find_parent("thead") or
                                     (cells and all(c.name == "th" for c in cells))):
            headers += 1
        rows.append([row.get(i, "") for i in range(max(row) + 1)])
    if not rows:
        return [], []
    width = max(map(len, rows))
    for row in rows:
        row.extend([""] * (width - len(row)))
    headers = headers or 1  # DART frequently uses TD even for the heading.
    heading = [" / ".join(dict.fromkeys(row[i] for row in rows[:headers] if row[i]))
               for i in range(width)]
    return heading, rows[headers:]


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("|", "\\|")


def _row_lines(row: list[str], budget: int) -> Iterator[str]:
    """Split an oversized cell without moving any fragment into another column."""
    escaped = [_escape(cell) for cell in row]
    if len(_line(escaped)) <= budget:
        yield _line(escaped)
        return
    capacity = budget - len(_line([""] * len(row)))
    if capacity < 1:
        raise ValueError("chunk limit cannot fit a data cell below the table header")
    part = [""] * len(row)
    free = capacity
    for i, cell in enumerate(row):
        for char in cell:
            char = _escape(char)
            if len(char) > capacity:
                raise ValueError("chunk limit cannot fit an escaped cell character")
            if len(char) > free:
                yield _line(part)
                part = [""] * len(row)
                free = capacity
            part[i] += char
            free -= len(char)
    yield _line(part)


def _table_chunks(table: Tag, limit: int) -> Iterator[str]:
    heading, rows = _table_rows(table)
    if not heading:
        return
    header = _line([_escape(cell) for cell in heading]) + "\n" + _line(["---"] * len(heading))
    if len(header) > limit:
        raise ValueError("chunk limit is shorter than the original table header")
    chunk = header
    for row in rows:
        for line in _row_lines(row, limit - len(header) - 1):
            if len(chunk) + 1 + len(line) > limit:
                yield chunk
                chunk = header
            chunk += "\n" + line
    yield chunk


def chunk_document(markup: str, max_chars: int = 1800) -> list[str]:
    if max_chars < 1:
        raise ValueError("max_chars must be positive")
    soup = BeautifulSoup(markup, "html.parser")
    for tag in soup(["script", "style"]):
        tag.decompose()
    chunks = []
    prose = []

    def flush():
        paragraph = " ".join(" ".join(prose).split())
        while paragraph:
            end = len(paragraph)
            if end > max_chars:
                end = paragraph.rfind(" ", 0, max_chars + 1)
                if end < 1:
                    end = max_chars
            chunks.append(paragraph[:end])
            paragraph = paragraph[end:].lstrip()
        prose.clear()

    for node in soup.descendants:
        if isinstance(node, Tag) and node.name == "table":
            flush()
            caption = node.find("caption", recursive=False)
            if caption:
                prose.append(caption.get_text(" ", strip=True))
                flush()
            chunks.extend(_table_chunks(node, max_chars))
        elif (isinstance(node, NavigableString)
              and not isinstance(node, (Comment, Declaration, ProcessingInstruction))
              and not node.find_parent("table")):
            prose.append(str(node))
    flush()
    return chunks
