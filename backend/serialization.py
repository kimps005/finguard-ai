"""One JSON encoding for API responses, SSE frames, caches and LLM prompts."""

from dataclasses import asdict, is_dataclass
from datetime import date
from decimal import Decimal
import json


def encode(value):
    # Decimals stay exact as plain strings ("1E+2" becomes "100") so clients never see floats.
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, date):
        return value.isoformat()
    if is_dataclass(value):
        return asdict(value)
    raise TypeError(f"Unexpected JSON value {type(value).__name__}")


def dump(value) -> str:
    return json.dumps(value, ensure_ascii=False, default=encode, allow_nan=False)
