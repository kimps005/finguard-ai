from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

import pytest

from backend.serialization import dump


@dataclass
class Row:
    price: Decimal
    day: date


def test_decimals_dates_and_dataclasses_are_exact_strings():
    assert dump({"a": Decimal("1E+2"), "b": Decimal("0.10"), "c": Decimal("-0")}) == '{"a": "100", "b": "0.10", "c": "-0"}'
    assert dump([date(2026, 11, 20), datetime(2026, 11, 20, 9, 5)]) == '["2026-11-20", "2026-11-20T09:05:00"]'
    assert dump(Row(Decimal("9637.15"), date(2026, 1, 2))) == '{"price": "9637.15", "day": "2026-01-02"}'
    assert dump({"종목": "삼성전자"}) == '{"종목": "삼성전자"}'


def test_floats_nan_and_unknown_types_are_rejected():
    with pytest.raises(ValueError):
        dump(float("nan"))
    with pytest.raises(TypeError):
        dump({1, 2})
