"""KRX trading-time rules shared by the broker, the portfolio graph and the price bands."""

from datetime import date, datetime, time, timedelta
from functools import lru_cache
from zoneinfo import ZoneInfo

import exchange_calendars


KST = ZoneInfo("Asia/Seoul")
# KRX finalizes daily bars and investor flows after the close; data before this is provisional.
CONFIRMATION_TIME = time(15, 40)


@lru_cache(maxsize=1)
def krx_calendar():
    return exchange_calendars.get_calendar("XKRX")


def confirmed_day(moment: datetime) -> date:
    """Return the latest calendar day whose KRX daily data is final at this moment."""
    local = moment.astimezone(KST)
    return local.date() - timedelta(days=local.time() < CONFIRMATION_TIME)


def candle_poll_delay(now: datetime) -> int:
    """Seconds until intraday candles can change: 15s in session, else until the next open."""
    calendar = krx_calendar()
    session = calendar.date_to_session(now.astimezone(KST).date(), direction="next")
    if now >= calendar.session_close(session) + timedelta(seconds=30):
        session = calendar.next_session(session)
    wait = calendar.session_open(session) - now
    return max(15, wait.days * 86400 + wait.seconds + 1)
