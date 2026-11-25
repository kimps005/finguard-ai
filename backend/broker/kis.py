"""Read-only Korea Investment & Securities connector for domestic accounts and KRX data."""

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, DecimalException
from hashlib import sha256
import json
import logging
import os

import httpx
from redis.asyncio import Redis
from redis.exceptions import RedisError

from backend.broker.candles import aggregate_ten_minute_candles, candle_period_start, candle_values, fold_bar
from backend.broker.models import (
    ZERO, Balance, BrokerConfigurationError, BrokerError, Candle, DailyBar, FiveDayFlow, Holding,
    InvestorFlow, Quote, RetryableBrokerError, day_string, master_identity, number, symbol,
)
from backend.broker.pykrx_source import PykrxMarketData
from backend.market_time import KST, confirmed_day, krx_calendar
from backend.stock_master import fetch_stocks


LIVE = "https://openapi.koreainvestment.com:9443"
VIRTUAL = "https://openapivts.koreainvestment.com:29443"
DOMESTIC = "/uapi/domestic-stock/v1"
RETRY_DELAYS = (0.25, 0.5, 1)
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Credentials:
    app_key: str = field(repr=False)
    app_secret: str = field(repr=False)
    account_no: str = field(default="", repr=False)
    product_code: str = "01"


@dataclass(frozen=True)
class BrokerSettings:
    live: Credentials
    virtual: Credentials
    is_virtual: bool

    @classmethod
    def from_env(cls):
        def credentials(prefix):
            return Credentials(*(os.getenv(prefix + name, default).strip() for name, default in (
                ("APP_KEY", ""), ("APP_SECRET", ""), ("ACCOUNT_NO", ""),
                ("ACCOUNT_PRODUCT_CODE", "01"),
            )))

        mode = os.getenv("IS_VIRTUAL", "true").strip().lower()
        if mode not in {"true", "false"}:
            raise ValueError("IS_VIRTUAL must be true or false")
        return cls(credentials("KIS_"), credentials("KIS_VIRTUAL_"), mode == "true")


class KisAccount:
    """KIS domestic account on the virtual or live domain, chosen by IS_VIRTUAL."""

    def __init__(self, connector):
        self.connector = connector

    async def get_balance(self) -> Balance:
        virtual = self.connector.settings.is_virtual
        domain, credentials, transaction = (
            (VIRTUAL, self.connector.settings.virtual, "VTTC8434R") if virtual
            else (LIVE, self.connector.settings.live, "TTTC8434R")
        )
        if not all((credentials.account_no, credentials.app_key, credentials.app_secret)):
            raise BrokerConfigurationError("kis_account_not_configured", "virtual" if virtual else "live")
        params = {
            "CANO": credentials.account_no, "ACNT_PRDT_CD": credentials.product_code,
            "AFHR_FLPR_YN": "N", "OFL_YN": "", "INQR_DVSN": "02", "UNPR_DVSN": "01",
            "FUND_STTL_ICLD_YN": "N", "FNCG_AMT_AUTO_RDPT_YN": "N", "PRCS_DVSN": "00",
        }
        rows, cursors = [], set()
        summary = None
        cursor = ("", "")
        continuation = ""
        while True:
            params["CTX_AREA_FK100"], params["CTX_AREA_NK100"] = cursor
            body, headers = await self.connector._get(domain, credentials, "/trading/inquire-balance",
                                            transaction, params, continuation)
            rows.extend(body["output1"])
            if summary is None and body.get("output2"):
                summary = body["output2"][0]
            if headers.get("tr_cont") not in {"F", "M"}:
                break
            cursor = tuple(body[field].strip() for field in ("ctx_area_fk100", "ctx_area_nk100"))
            if not any(cursor) or cursor in cursors:
                raise BrokerError("Invalid KIS balance continuation")
            cursors.add(cursor)
            continuation = "N"
        if summary is None:
            raise BrokerError("KIS balance summary is missing")
        cash, total = (number(summary[field]) for field in ("dnca_tot_amt", "tot_evlu_amt"))
        positions = [(row, number(row["hldg_qty"])) for row in rows]
        positions = [(row, qty) for row, qty in positions if qty != ZERO]
        master = await self.connector.stock_master(list(dict.fromkeys(symbol(row["pdno"]) for row, _ in positions)))
        holdings = []
        for row, quantity in positions:
            code = symbol(row["pdno"])
            asset_type, corp_code, isin = master_identity(master.get(code))
            avg, price, cost, value, pnl, pnl_pct = (number(row[field]) for field in (
                "pchs_avg_pric", "prpr", "pchs_amt", "evlu_amt", "evlu_pfls_amt", "evlu_pfls_rt",
            ))
            holdings.append(Holding(code, row["prdt_name"], asset_type, corp_code, isin, quantity,
                                    avg, price, cost, value, pnl, pnl_pct, value / total if total else ZERO))
        return Balance(cash, total, cash / total if total else ZERO, tuple(holdings))

    async def get_orderable_cash(self, holding: Holding | None = None) -> Decimal:
        domain, credentials, tr_id = (
            (VIRTUAL, self.connector.settings.virtual, "VTTC8908R") if self.connector.settings.is_virtual
            else (LIVE, self.connector.settings.live, "TTTC8908R"))
        payload, _ = await self.connector._get(
            domain, credentials, "/trading/inquire-psbl-order", tr_id,
            {"CANO": credentials.account_no, "ACNT_PRDT_CD": credentials.product_code,
             "PDNO": holding.stock_code if holding else "",
             "ORD_UNPR": format(holding.current_price, "f") if holding else "0",
             "ORD_DVSN": "00", "CMA_EVLU_AMT_ICLD_YN": "N", "OVRS_ICLD_YN": "N"})
        return number(payload["output"]["ord_psbl_cash"])


class BrokerConnector:
    """Account access plus KRX market data. Share one instance per process for the semaphore.

    The account source is pluggable (KIS by default, Toss via toss_account); quotes, bars
    and investor flows always use the KIS live domain with a PyKRX fallback.
    """

    def __init__(self, settings, redis, http, *, account=None, market_data=None, stock_master=fetch_stocks,
                 clock=None, sleep=asyncio.sleep):
        self.settings = settings
        self.redis = redis
        self.http = http
        self.market_data = market_data if market_data is not None else PykrxMarketData()
        self.stock_master = stock_master
        self.clock = clock if clock is not None else lambda: datetime.now(timezone.utc)
        self.sleep = sleep
        self.semaphore = asyncio.Semaphore(2)
        self.account = (account or KisAccount)(self)

    @classmethod
    @asynccontextmanager
    async def from_env(cls, **kwargs):
        settings = BrokerSettings.from_env()
        redis = Redis.from_url(os.getenv("REDIS_URL") or "redis://127.0.0.1:6379/0",
                               socket_timeout=5, socket_connect_timeout=5)
        async with redis, httpx.AsyncClient(timeout=5) as http:
            yield cls(settings, redis, http, **kwargs)

    async def _retry(self, operation):
        for delay in (*RETRY_DELAYS, None):
            try:
                return await operation()
            except (httpx.TransportError, RetryableBrokerError) as exc:
                if delay is None:
                    raise BrokerError("KIS request exhausted its retries") from exc
                await self.sleep(delay)

    async def _send(self, method, url, **kwargs):
        async with self.semaphore:
            response = await self.http.request(method, url, **kwargs)
        status = response.status_code
        retryable = status in {408, 429} or status >= 500
        try:
            body = response.json(parse_float=Decimal)
        except ValueError as exc:
            if retryable:
                raise RetryableBrokerError(f"KIS HTTP {status}") from exc
            raise BrokerError(f"Invalid KIS response (HTTP {status})") from exc
        if not isinstance(body, dict):
            if retryable:
                raise RetryableBrokerError(f"KIS HTTP {status}")
            raise BrokerError("Invalid KIS response object")
        code = body.get("msg_cd") or body.get("error_code")
        result = body.get("rt_cd", "0")
        if retryable or code in {"EGW00201", "EGW00133"} or status >= 400 or result != "0":
            detail = f"KIS HTTP {status}, rt_cd={result}, msg_cd={code}"
            if "/quotations/" in url:
                detail += f", msg1={body.get('msg1')}"
            if retryable or code in {"EGW00201", "EGW00133"}:
                raise RetryableBrokerError(detail)
            raise BrokerError(detail)
        return body, response.headers

    async def _token(self, domain, credentials):
        if not credentials.app_key or not credentials.app_secret:
            raise BrokerError("KIS app credentials are missing")
        fingerprint = sha256(f"{credentials.app_key}\0{credentials.app_secret}".encode()).hexdigest()
        key = f"finguard:kis:oauth:{domain}:{fingerprint}"

        async def issue():
            body, _ = await self._send("POST", domain + "/oauth2/tokenP", json={
                "grant_type": "client_credentials",
                "appkey": credentials.app_key, "appsecret": credentials.app_secret,
            })
            return body

        return await self._cached_oauth(key, issue)

    async def _cached_oauth(self, key, issue):
        async def cached():
            raw = await self.redis.get(key)
            if raw:
                try:
                    item = json.loads(raw)
                    expiry = datetime.fromisoformat(item["expires_at"])
                    if expiry > self.clock() + timedelta(seconds=60):
                        return item["access_token"]
                except (KeyError, ValueError, TypeError):
                    pass
            return None

        try:
            token = await cached()
            if token:
                return token
            async with self.redis.lock(key + ":refresh", timeout=120, blocking_timeout=35):
                token = await cached()
                if token:
                    return token
                issued = self.clock()
                body = await self._retry(issue)
                try:
                    token, ttl = body["access_token"], int(body["expires_in"])
                    if not isinstance(token, str) or not token or ttl <= 0:
                        raise ValueError
                except (KeyError, ValueError, TypeError) as exc:
                    raise BrokerError("Invalid broker OAuth response") from exc
                expiry = issued + timedelta(seconds=ttl)
                await self.redis.set(key, json.dumps({
                    "access_token": token, "expires_at": expiry.isoformat(),
                }), ex=ttl)
                return token
        except RedisError as exc:
            raise BrokerError("Broker OAuth Redis cache is unavailable") from exc

    async def _drop_oauth(self, key, token):
        """Forget a token the broker rejected, unless another request already replaced it."""
        try:
            async with self.redis.lock(key + ":refresh", timeout=120, blocking_timeout=35):
                raw = await self.redis.get(key)
                try:
                    current = json.loads(raw)["access_token"] if raw else None
                except (KeyError, ValueError, TypeError):
                    current = token
                if current == token:
                    await self.redis.delete(key)
        except RedisError as exc:
            raise BrokerError("Broker OAuth Redis cache is unavailable") from exc

    async def _get(self, domain, credentials, path, transaction_id, params, continuation=""):
        token = await self._token(domain, credentials)
        headers = {
            "authorization": f"Bearer {token}", "appkey": credentials.app_key,
            "appsecret": credentials.app_secret, "tr_id": transaction_id,
            "custtype": "P", "tr_cont": continuation,
        }
        return await self._retry(lambda: self._send(
            "GET", domain + DOMESTIC + path, headers=headers, params=params
        ))

    async def get_balance(self) -> Balance:
        return await self.account.get_balance()

    async def get_orderable_cash(self, holding: Holding | None = None) -> Decimal:
        return await self.account.get_orderable_cash(holding)

    async def _market(self, endpoint, transaction, code, params=None):
        code_params = {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code}
        body, _ = await self._get(LIVE, self.settings.live, "/quotations/" + endpoint,
                                  transaction, code_params | (params or {}))
        return body

    @staticmethod
    def _market_symbol(stock_code, asset_type):
        code = symbol(stock_code)
        if asset_type is None and stock_code.strip().startswith("Q") and len(code) == 6:
            asset_type = "ETN"
        return ("Q" + code if asset_type == "ETN" else code), asset_type

    async def _with_fallback(self, primary, fallback, *args):
        try:
            return await primary()
        except (BrokerError, KeyError, TypeError, ValueError, DecimalException):
            try:
                async with self.semaphore:
                    return await asyncio.to_thread(fallback, *args)
            except Exception as exc:
                raise BrokerError("PyKRX fallback failed") from exc

    async def get_quote(self, stock_code: str, *, asset_type=None) -> Quote:
        code = symbol(stock_code)
        kis_code, asset_type = self._market_symbol(stock_code, asset_type)

        async def primary():
            body = await self._market("inquire-price", "FHKST01010100", kis_code)
            output = body["output"]
            try:
                change = number(output["prdy_ctrt"])
            except (KeyError, ValueError, DecimalException):
                change = None
            price = number(output["stck_prpr"])
            if price <= 0:
                raise BrokerError("KIS returned no positive price")
            return Quote(code, price, "kis", change_pct=change)

        def fallback():
            end = self.clock().astimezone(KST).date()
            bars = sorted((bar for bar in self.market_data.daily(
                code, end - timedelta(days=14), end, asset_type) if bar.close > 0),
                key=lambda bar: bar.session)
            if not bars:
                raise BrokerError("PyKRX returned no price")
            latest = bars[-1]
            change = (latest.close - bars[-2].close) * 100 / bars[-2].close if len(bars) > 1 else None
            return Quote(code, latest.close, "pykrx", latest.session, change)

        return await self._with_fallback(primary, fallback)

    async def get_quotes(self, stock_codes) -> tuple[Quote, ...]:
        return tuple(await asyncio.gather(*(self.get_quote(code) for code in stock_codes)))

    async def get_daily_bars(self, stock_code: str, start: date, end: date, *,
                             asset_type=None) -> tuple[DailyBar, ...]:
        if start > end:
            raise ValueError("start must be on or before end")
        kis_code, asset_type = self._market_symbol(stock_code, asset_type)

        async def primary():
            bars = {}
            page_start = start
            while page_start <= end:
                page_end = min(page_start + timedelta(days=99), end)
                body = await self._market(
                    "inquire-daily-itemchartprice", "FHKST03010100", kis_code, {
                        "FID_INPUT_DATE_1": day_string(page_start),
                        "FID_INPUT_DATE_2": day_string(page_end), "FID_PERIOD_DIV_CODE": "D",
                        "FID_ORG_ADJ_PRC": "0" if asset_type == "ETN" else "1",
                    },
                )
                for row in body["output2"]:
                    if not row.get("stck_bsop_date"):
                        continue
                    session = date.fromisoformat(row["stck_bsop_date"])
                    if page_start <= session <= page_end:
                        bars[session] = DailyBar(session, *(number(row[field]) for field in (
                            "stck_oprc", "stck_hgpr", "stck_lwpr", "stck_clpr", "acml_vol",
                        )))
                page_start = page_end + timedelta(days=1)
            if not bars:
                raise BrokerError("KIS returned no daily bars")
            return tuple(bars[session] for session in sorted(bars))

        bars = await self._with_fallback(primary, self.market_data.daily, symbol(stock_code),
                                         start, end, asset_type)
        return tuple(sorted((bar for bar in bars if start <= bar.session <= end),
                            key=lambda bar: bar.session))

    async def get_candle_history(self, stock_code: str, period: str, *, recent=False) -> list[Candle]:
        code, asset_type = self._market_symbol(stock_code, None)
        today = self.clock().astimezone(KST).date()
        start = today - timedelta(days=30) if recent else date(1956, 1, 1)
        end = today
        candles = {}
        for page_number in range(15):
            if end < start:
                break
            if page_number:
                await self.sleep(0.15)
            try:
                body = await self._market("inquire-daily-itemchartprice", "FHKST03010100", code, {
                    "FID_INPUT_DATE_1": day_string(start), "FID_INPUT_DATE_2": day_string(end),
                    "FID_PERIOD_DIV_CODE": period, "FID_ORG_ADJ_PRC": "0",
                })
            except BrokerError as exc:
                logger.warning("KIS %s candle history failed: %s", period, exc.__cause__ or exc)
                break
            dates = []
            for row in body.get("output2") or ():
                try:
                    session = date.fromisoformat(row.get("stck_bsop_date", ""))
                except (TypeError, ValueError):
                    continue
                if not start <= session <= end:
                    continue
                dates.append(session)
                values = candle_values(row, "stck_clpr", "acml_vol")
                if values is not None:
                    # Keep the fuller bar if the next request ends inside the same period.
                    bucket = candle_period_start(session, period)
                    candles.setdefault(bucket, Candle(bucket, *values))
            if not dates:
                break
            end = candle_period_start(min(dates), period) - timedelta(days=1)
        if candles:
            return [candles[bucket] for bucket in sorted(candles)]

        fallback_days = 30 if recent else {"D": 365, "W": 1825, "Y": 5475}[period]
        fallback_options = {"adjusted": True} if period == "Y" else {}
        try:
            async with self.semaphore:
                bars = await asyncio.to_thread(self.market_data.daily, symbol(stock_code),
                                               today - timedelta(days=fallback_days), today, asset_type,
                                               **fallback_options)
        except Exception as exc:
            raise BrokerError("PyKRX candle fallback failed") from exc
        for bar in sorted({bar.session: bar for bar in bars}.values(), key=lambda bar: bar.session):
            prices = (bar.open, bar.high, bar.low, bar.close)
            if not all(price.is_finite() and price > ZERO for price in prices):
                continue
            fold_bar(candles, candle_period_start(bar.session, period), *prices, bar.volume)
        return [candles[bucket] for bucket in sorted(candles)]

    async def get_ten_minute_candles(self, stock_code: str) -> list[Candle]:
        code, _ = self._market_symbol(stock_code, None)
        now = self.clock().astimezone(KST)
        calendar = krx_calendar()
        session = calendar.date_to_session(now.date(), direction="previous")
        if now < calendar.session_open(session):
            session = calendar.previous_session(session)
        candles = {}
        complete_sessions = 0
        # A current-session page can contain only 30 one-minute bars.
        pages = iter(range(5 * 14))
        for offset in range(5):
            start = calendar.session_open(session).astimezone(KST).to_pydatetime()
            close = calendar.session_close(session).astimezone(KST).to_pydatetime()
            try:
                if now >= close:
                    bars, complete = await self._completed_minute_session(code, start, close, pages)
                else:
                    bars, complete = await self._minute_candles(code, start, now, historical=False, pages=pages)
            except BrokerError as exc:
                logger.warning("KIS 10m candle history failed: %s", exc.__cause__ or exc)
                break
            candles.update((bar.time, bar) for bar in bars)
            if bars and complete:
                complete_sessions += 1
            if offset < 4:
                session = calendar.previous_session(session)
        if complete_sessions < 5:
            for bar in await self._public_ten_minute_candles(symbol(stock_code)):
                candles.setdefault(bar.time, bar)
        dates = {stamp: datetime.fromtimestamp(stamp, KST).date() for stamp in candles}
        recent_sessions = set(sorted(set(dates.values()))[-5:])
        return [candles[stamp] for stamp in sorted(candles) if dates[stamp] in recent_sessions]

    async def _public_ten_minute_candles(self, code: str) -> list[Candle]:
        fields = ("open", "high", "low", "close", "volume")
        for exchange in ("KS", "KQ"):
            try:
                async with self.semaphore:
                    response = await self.http.get(
                        f"https://query1.finance.yahoo.com/v8/finance/chart/{code}.{exchange}",
                        params={"range": "5d", "interval": "5m", "includePrePost": "false"},
                        headers={"User-Agent": "Mozilla/5.0"},
                    )
                response.raise_for_status()
                result = response.json(parse_float=Decimal)["chart"]["result"][0]
                if result["meta"]["dataGranularity"] != "5m":
                    continue
                quote = result["indicators"]["quote"][0]
                rows = zip(result["timestamp"], *(quote[field] for field in fields), strict=True)
                bars = {}
                for stamp, *values in rows:
                    if type(stamp) is not int or None in values:
                        continue
                    prices = tuple(map(number, values))
                    if min(prices[:4]) > ZERO and prices[4] >= ZERO:
                        bars[stamp] = prices
                if bars:
                    return aggregate_ten_minute_candles(bars)
            except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError, DecimalException) as exc:
                logger.warning("Public 5m candle lookup failed for %s.%s (%s)", code, exchange,
                               type(exc).__name__)
        return []

    async def _completed_minute_session(self, code: str, start: datetime, close: datetime,
                                        pages) -> tuple[list[Candle], bool]:
        key = f"finguard:candles:10m-session:{code}:{start.date()}"
        try:
            cached = await self.redis.get(key)
            if cached:
                bars = []
                for stamp, *values in json.loads(cached):
                    open_, high, low, close_, volume = map(number, values)
                    if type(stamp) is int and min(open_, high, low, close_) > ZERO and volume >= ZERO:
                        bars.append(Candle(stamp, open_, high, low, close_, volume))
                if bars:
                    return bars, True
        except (RedisError, ValueError, TypeError, DecimalException):
            pass
        candles, complete = await self._minute_candles(code, start, close, historical=True, pages=pages)
        if not candles or not complete:
            return candles, complete
        payload = [(bar.time, bar.open, bar.high, bar.low, bar.close, bar.volume) for bar in candles]
        try:
            await self.redis.set(key, json.dumps(payload, default=str), ex=86400)
        except RedisError:
            pass
        return candles, complete

    async def _minute_candles(self, code: str, start: datetime, cursor: datetime, *,
                              historical: bool, pages) -> tuple[list[Candle], bool]:
        endpoint, tr_id = "inquire-time-itemchartprice", "FHKST03010200"
        params = {"FID_PW_DATA_INCU_YN": "Y", "FID_ETC_CLS_CODE": ""}
        if historical:
            endpoint, tr_id = "inquire-time-dailychartprice", "FHKST03010230"
            params = {"FID_PW_DATA_INCU_YN": "Y", "FID_FAKE_TICK_INCU_YN": "",
                      "FID_INPUT_DATE_1": day_string(cursor.date())}

        session = cursor.date()
        minutes = {}
        complete = False
        for page_number in pages:
            if page_number:
                await self.sleep(0.15)
            try:
                body = await self._market(endpoint, tr_id, code,
                                          params | {"FID_INPUT_HOUR_1": cursor.strftime("%H%M%S")})
            except BrokerError as exc:
                if not minutes:
                    raise
                logger.warning("KIS 10m candle pagination failed: %s", exc.__cause__ or exc)
                break
            page = {}
            timestamps = []
            for row in body.get("output2") or ():
                if row.get("stck_bsop_date") != day_string(session) or not row.get("stck_cntg_hour"):
                    continue
                try:
                    stamp = datetime.strptime(row["stck_bsop_date"] + row["stck_cntg_hour"],
                                              "%Y%m%d%H%M%S").replace(tzinfo=KST)
                except (TypeError, ValueError):
                    continue
                if not start <= stamp <= cursor:
                    continue
                timestamps.append(stamp)
                values = candle_values(row, "stck_prpr", "cntg_vol")
                if values is not None:
                    page[int(stamp.timestamp())] = values
            if not timestamps:
                complete = True
                break
            minutes.update(page)
            cursor = min(timestamps) - timedelta(seconds=1)
            if cursor < start:
                complete = True
                break

        return aggregate_ten_minute_candles(minutes), complete

    async def get_five_day_flow(self, stock_code: str, *, as_of: datetime | None = None,
                                 asset_type=None) -> FiveDayFlow:
        """Aggregate net buy quantities in shares over five completed KRX sessions."""
        as_of = self.clock() if as_of is None else as_of
        if as_of.utcoffset() is None:
            raise ValueError("as_of must be timezone-aware")
        kis_code, asset_type = self._market_symbol(stock_code, asset_type)
        calendar = krx_calendar()
        last = calendar.date_to_session(confirmed_day(as_of), direction="previous")
        sessions = [last.date()]
        for _ in range(4):
            last = calendar.previous_session(last)
            sessions.append(last.date())
        sessions.reverse()

        def aggregate(rows):
            by_date = {row.session: row for row in rows}
            if not all(session in by_date for session in sessions):
                raise BrokerError("Five confirmed KRX sessions are not available")
            days = tuple(by_date[session] for session in sessions)
            return FiveDayFlow(days, sum((row.foreign_net_buy for row in days), ZERO),
                               sum((row.institution_net_buy for row in days), ZERO))

        async def primary():
            body = await self._market("inquire-investor", "FHKST01010900", kis_code)
            rows = []
            for row in body["output"]:
                if not row.get("stck_bsop_date"):
                    continue
                session = date.fromisoformat(row["stck_bsop_date"])
                if session in sessions:
                    rows.append(InvestorFlow(session, number(row["frgn_ntby_qty"]),
                                             number(row["orgn_ntby_qty"])))
            return aggregate(rows)

        def fallback():
            return aggregate(self.market_data.flow(symbol(stock_code), sessions[0],
                                                   sessions[-1], asset_type))

        return await self._with_fallback(primary, fallback)
