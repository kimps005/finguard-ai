"""PyKRX fallback for KRX daily bars and investor flows."""

import os
import threading
import time as wall_clock

from backend.broker.models import DailyBar, InvestorFlow, day_string, number


class PykrxMarketData:
    """Synchronous adapter. BrokerConnector runs its calls off the event loop."""

    _login_lock = threading.Lock()

    def keep_session(self, margin=0):
        """Log in to KRX once, serialized, and renew it when fewer than margin seconds remain."""
        from pykrx.website.comm import auth

        with self._login_lock:
            session = auth.get_auth_session()
            if session is not None and session.expiry_time - wall_clock.time() < margin:
                session.refresh(os.getenv("KRX_ID"), os.getenv("KRX_PW"))

    def daily(self, code, start, end, asset_type=None, *, adjusted=False):
        from pykrx import stock

        self.keep_session()

        args = (day_string(start), day_string(end), code)
        if asset_type == "ETF" and not adjusted:
            frame = stock.get_etf_ohlcv_by_date(*args)
        else:
            # PyKRX's adjusted route uses Naver, which accepts ETN tickers.
            frame = stock.get_market_ohlcv_by_date(*args, adjusted=adjusted or asset_type == "ETN")
        if frame.empty:
            return ()
        columns = ["시가", "고가", "저가", "종가", "거래량"]
        return tuple(DailyBar(index.date(), *(number(value) for value in values))
                     for index, *values in frame[columns].itertuples(name=None))

    def flow(self, code, start, end, asset_type=None):
        from pykrx import stock

        self.keep_session()

        args = (day_string(start), day_string(end), code)
        if asset_type in {"ETF", "ETN"}:
            frame = stock.get_etf_trading_volume_and_value(*args, "거래량", "순매수")
            columns = ["외국인", "기관"]
        else:
            frame = stock.get_market_trading_volume_by_date(*args)
            columns = ["외국인합계", "기관합계"]
        if frame.empty:
            return ()
        return tuple(InvestorFlow(index.date(), number(foreign), number(institution))
                     for index, foreign, institution in frame[columns].itertuples(name=None))
