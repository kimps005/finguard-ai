from datetime import date, datetime, timezone

import pytest

from backend.market_time import KST, confirmed_day


@pytest.mark.parametrize("moment,expected", [
    (datetime(2026, 11, 20, 15, 39, 59, 999999, tzinfo=KST), date(2026, 11, 19)),
    (datetime(2026, 11, 20, 15, 40, tzinfo=KST), date(2026, 11, 20)),
    (datetime(2026, 11, 20, 0, 0, tzinfo=KST), date(2026, 11, 19)),
    # 06:40 UTC is 15:40 KST; the rule applies on the Korean calendar.
    (datetime(2026, 11, 20, 6, 40, tzinfo=timezone.utc), date(2026, 11, 20)),
    # 15:30 UTC is 00:30 KST on the next day, after the previous day's confirmation.
    (datetime(2026, 11, 20, 15, 30, tzinfo=timezone.utc), date(2026, 11, 20)),
])
def test_confirmed_day_switches_at_1540_kst(moment, expected):
    assert confirmed_day(moment) == expected
