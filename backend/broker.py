"""Read-only Korea Investment & Securities connector for domestic accounts and KRX data."""

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, DecimalException
from functools import lru_cache
from hashlib import sha256
import json
import os
from zoneinfo import ZoneInfo

import exchange_calendars
import httpx
import psycopg
from redis.asyncio import Redis
from redis.exceptions import RedisError


KST = ZoneInfo("Asia/Seoul")
LIVE = "https://openapi.koreainvestment.com:9443"
VIRTUAL = "https://openapivts.koreainvestment.com:29443"
DOMESTIC = "/uapi/domestic-stock/v1"
ZERO = Decimal(0)
RETRY_DELAYS = (0.25, 0.5, 1)


class BrokerError(RuntimeError):
    pass


class BrokerConfigurationError(BrokerError):
    def __init__(self, code: str, mode: str):
        self.code = code
        self.mode = mode
        super().__init__(f"{code} ({mode})")


class RetryableBrokerError(BrokerError):
    pass


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


@dataclass(frozen=True)
class Holding:
    stock_code: str
    name: str
    asset_type: str | None
    corp_code: str | None
    isin_cd: str | None
    quantity: Decimal
    average_price: Decimal
    current_price: Decimal
    purchase_amount: Decimal
    market_value: Decimal
    profit_loss: Decimal
    profit_loss_pct: Decimal
    weight: Decimal


@dataclass(frozen=True)
class Balance:
    cash_deposit: Decimal | None
    total_value: Decimal
    cash_weight: Decimal | None
    holdings: tuple[Holding, ...]


@dataclass(frozen=True)
class Quote:
    stock_code: str
    current_price: Decimal
    source: str
    session: date | None = None


@dataclass(frozen=True)
class DailyBar:
    session: date
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal


@dataclass(frozen=True)
class InvestorFlow:
    session: date
    foreign_net_buy: Decimal
    institution_net_buy: Decimal


@dataclass(frozen=True)
class FiveDayFlow:
    days: tuple[InvestorFlow, ...]
    foreign_net_buy: Decimal
    institution_net_buy: Decimal

    @property
    def as_of(self) -> date:
        return self.days[-1].session


def number(value) -> Decimal:
    parsed = Decimal(str(value).strip().replace(",", ""))
    if not parsed.is_finite():
        raise ValueError("Non-finite broker number")
    return parsed


def symbol(code: str) -> str:
    code = code.strip()
    if len(code) == 7 and code[0] in {"A", "Q"}:
        return code[1:]
    return code


def day_string(value: date) -> str:
    return value.strftime("%Y%m%d")


@lru_cache(maxsize=1)
def krx_calendar():
    return exchange_calendars.get_calendar("XKRX")


async def load_stock_master(codes: list[str]) -> dict:
    if not codes:
        return {}
    async with await psycopg.AsyncConnection.connect() as connection:
        cursor = await connection.execute(
            "SELECT stock_code, asset_type, corp_code, isin_cd FROM stock_master "
            "WHERE stock_code = ANY(%s)", (codes,)
        )
        return {code: (asset_type, corp_code, isin) for code, asset_type, corp_code, isin
                in await cursor.fetchall()}


class PykrxMarketData:
    """Synchronous adapter. BrokerConnector runs its calls off the event loop."""

    def daily(self, code, start, end, asset_type=None):
        from pykrx import stock

        args = (day_string(start), day_string(end), code)
        if asset_type == "ETF":
            frame = stock.get_etf_ohlcv_by_date(*args)
        else:
            # PyKRX's adjusted route uses Naver, which accepts ETN tickers.
            frame = stock.get_market_ohlcv_by_date(*args, adjusted=asset_type == "ETN")
        if frame.empty:
            return ()
        columns = ["시가", "고가", "저가", "종가", "거래량"]
        return tuple(DailyBar(index.date(), *(number(value) for value in values))
                     for index, *values in frame[columns].itertuples(name=None))

    def flow(self, code, start, end, asset_type=None):
        from pykrx import stock

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


class BrokerConnector:
    """Share one instance per backend process to share the request semaphore."""

    def __init__(self, settings, redis, http, *, market_data=None, stock_master=load_stock_master,
                 clock=None, sleep=asyncio.sleep):
        self.settings = settings
        self.redis = redis
        self.http = http
        self.market_data = market_data if market_data is not None else PykrxMarketData()
        self.stock_master = stock_master
        self.clock = clock if clock is not None else lambda: datetime.now(timezone.utc)
        self.sleep = sleep
        self.semaphore = asyncio.Semaphore(2)

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
        if status in {408, 429} or status >= 500:
            raise RetryableBrokerError(f"KIS HTTP {status}")
        try:
            body = response.json(parse_float=Decimal)
        except ValueError as exc:
            raise BrokerError(f"Invalid KIS response (HTTP {status})") from exc
        if not isinstance(body, dict):
            raise BrokerError("Invalid KIS response object")
        code = body.get("msg_cd") or body.get("error_code")
        if code in {"EGW00201", "EGW00133"}:
            raise RetryableBrokerError(f"KIS rate limit ({code})")
        if status >= 400 or body.get("rt_cd", "0") != "0":
            raise BrokerError(f"KIS rejected request (HTTP {status}, code {code})")
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
        virtual = self.settings.is_virtual
        domain, credentials, transaction = (
            (VIRTUAL, self.settings.virtual, "VTTC8434R") if virtual
            else (LIVE, self.settings.live, "TTTC8434R")
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
            body, headers = await self._get(domain, credentials, "/trading/inquire-balance",
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
        master = await self.stock_master(list(dict.fromkeys(symbol(row["pdno"]) for row, _ in positions)))
        holdings = []
        for row, quantity in positions:
            code = symbol(row["pdno"])
            asset_type, corp_code, isin = master.get(code, (None, None, None))
            avg, price, cost, value, pnl, pnl_pct = (number(row[field]) for field in (
                "pchs_avg_pric", "prpr", "pchs_amt", "evlu_amt", "evlu_pfls_amt", "evlu_pfls_rt",
            ))
            holdings.append(Holding(code, row["prdt_name"], asset_type, corp_code, isin, quantity,
                                    avg, price, cost, value, pnl, pnl_pct, value / total if total else ZERO))
        return Balance(cash, total, cash / total if total else ZERO, tuple(holdings))

    async def get_orderable_cash(self, holding: Holding | None = None) -> Decimal:
        domain, credentials, tr_id = (
            (VIRTUAL, self.settings.virtual, "VTTC8908R") if self.settings.is_virtual
            else (LIVE, self.settings.live, "TTTC8908R"))
        payload, _ = await self._get(
            domain, credentials, "/trading/inquire-psbl-order", tr_id,
            {"CANO": credentials.account_no, "ACNT_PRDT_CD": credentials.product_code,
             "PDNO": holding.stock_code if holding else "",
             "ORD_UNPR": format(holding.current_price, "f") if holding else "0",
             "ORD_DVSN": "00", "CMA_EVLU_AMT_ICLD_YN": "N", "OVRS_ICLD_YN": "N"})
        return number(payload["output"]["ord_psbl_cash"])

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
            return Quote(code, number(body["output"]["stck_prpr"]), "kis")

        def fallback():
            end = self.clock().astimezone(KST).date()
            bars = self.market_data.daily(code, end - timedelta(days=14), end, asset_type)
            if not bars:
                raise BrokerError("PyKRX returned no price")
            latest = max(bars, key=lambda bar: bar.session)
            return Quote(code, latest.close, "pykrx", latest.session)

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

    async def get_five_day_flow(self, stock_code: str, *, as_of: datetime | None = None,
                                 asset_type=None) -> FiveDayFlow:
        """Aggregate net buy quantities in shares over five completed KRX sessions."""
        as_of = self.clock() if as_of is None else as_of
        if as_of.utcoffset() is None:
            raise ValueError("as_of must be timezone-aware")
        kis_code, asset_type = self._market_symbol(stock_code, asset_type)
        local = as_of.astimezone(KST)
        cutoff = local.date() - timedelta(days=local.time() < time(15, 40))
        calendar = krx_calendar()
        last = calendar.date_to_session(cutoff, direction="previous")
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
