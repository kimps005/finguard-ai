import asyncio
from dataclasses import fields
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import json
from unittest.mock import AsyncMock
from urllib.parse import parse_qs

import httpx
import pandas as pd
import pytest

from backend.broker import (
    KST, LIVE, VIRTUAL, BrokerConfigurationError, BrokerConnector, BrokerError, BrokerSettings, KisAccount,
)
from backend.main import app
from backend.portfolio_market import MarketData
from backend.sync_stock_master import Stock
from backend.toss_broker import TossAccount, TossSettings, toss_account


class MemoryRedis:
    def __init__(self):
        self.values = {}
        self.locks = {}

    async def get(self, key):
        return self.values.get(key, (None, None))[0]

    async def set(self, key, value, *, ex):
        self.values[key] = (value, ex)

    async def delete(self, key):
        return int(self.values.pop(key, None) is not None)

    def lock(self, name, **kwargs):
        return self.locks.setdefault(name, asyncio.Lock())


MONDAY = date(2026, 10, 12)
FLOW_SESSIONS = [date(2026, 10, day) for day in (1, 2, 6, 7, 8, 12)]
BALANCE_ROW = {
    "prdt_name": "테스트 종목", "hldg_qty": "3", "pchs_avg_pric": "10.123456789123456789",
    "prpr": "12.5", "pchs_amt": "30.370370367370370367", "evlu_amt": "37.50",
    "evlu_pfls_amt": "-2.05", "evlu_pfls_rt": "-5.10",
}


@pytest.fixture
def settings(monkeypatch):
    for name, value in {
        "IS_VIRTUAL": "true",
        "KIS_APP_KEY": "live-key", "KIS_APP_SECRET": "live-secret",
        "KIS_ACCOUNT_NO": "12345678", "KIS_ACCOUNT_PRODUCT_CODE": "01",
        "KIS_VIRTUAL_APP_KEY": "vts-key", "KIS_VIRTUAL_APP_SECRET": "vts-secret",
        "KIS_VIRTUAL_ACCOUNT_NO": "87654321", "KIS_VIRTUAL_ACCOUNT_PRODUCT_CODE": "02",
    }.items():
        monkeypatch.setenv(name, value)
    return BrokerSettings.from_env()


def stock(code, asset_type, corp_code):
    return Stock(code, corp_code, "KR7" + code + "000", None, "종목" + code, asset_type, None, Decimal(1000))


def market_rows():
    return [
        {"stck_bsop_date": session.strftime("%Y%m%d"), "frgn_ntby_qty": str(index),
         "orgn_ntby_qty": str(-index)}
        for index, session in reversed(list(enumerate(FLOW_SESSIONS, 1)))
    ]


def test_toss_snapshot_and_cash_use_documented_read_only_api(settings, monkeypatch):
    paths = []
    position = {
        "symbol": "005930", "name": "Fixture", "marketCountry": "KR", "currency": "KRW",
        "quantity": "2.5", "lastPrice": "100.25", "averagePurchasePrice": "80.125",
        "marketValue": {"purchaseAmount": "200.3125", "amount": "250.625"},
        "profitLoss": {"amount": "50.3125", "rate": "0.25117"},
    }
    master = AsyncMock(return_value={"005930": stock("005930", "STOCK", "00123456")})

    def handle(request):
        path = request.url.path
        paths.append(path)
        assert request.url.host == "openapi.tossinvest.com"
        if path == "/oauth2/token":
            assert request.method == "POST"
            assert request.headers["Content-Type"] == "application/x-www-form-urlencoded"
            assert parse_qs(request.content.decode()) == {
                "grant_type": ["client_credentials"], "client_id": ["client-id"],
                "client_secret": ["client-secret"],
            }
            return httpx.Response(200, json={"access_token": "fixture", "expires_in": 3600})
        assert request.method == "GET"
        assert request.headers["Authorization"] == "Bearer fixture"
        if path == "/api/v1/accounts":
            assert "X-Tossinvest-Account" not in request.headers
            return httpx.Response(200, json={"result": [{"accountSeq": 7, "accountType": "BROKERAGE"}]})
        assert request.headers["X-Tossinvest-Account"] == "7"
        if path == "/api/v1/holdings":
            return httpx.Response(200, json={"result": {
                "marketValue": {"amount": {"krw": "250.625", "usd": "10000"}},
                "items": [position, position | {"symbol": "AAPL", "marketCountry": "US", "currency": "USD"}],
            }})
        assert path == "/api/v1/buying-power" and request.url.params["currency"] == "KRW"
        return httpx.Response(200, json={"result": {"currency": "KRW", "cashBuyingPower": "1234.56"}})

    async def read():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
            connector = BrokerConnector(settings, MemoryRedis(), http,
                                        account=toss_account(TossSettings("client-id", "client-secret")),
                                        stock_master=master)
            monkeypatch.setattr(app.state, "market", MarketData(connector, None, None), raising=False)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                         base_url="http://local") as client:
                return await client.get("/api/v1/account")

    response = asyncio.run(read())
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert paths == ["/oauth2/token", "/api/v1/accounts", "/api/v1/holdings", "/api/v1/buying-power"]
    master.assert_awaited_once_with(["005930"])
    snapshot = response.json()
    assert snapshot["account_summary"] == {
        "total_value": "250.625", "cash_deposit": None, "cash_weight": None, "orderable_cash": "1234.56",
    }
    holding, = snapshot["holdings"]
    assert holding["profit_loss_pct"] == "25.11700" and holding["quantity"] == "2.5"
    assert holding["weight"] == "1" and holding["corp_code"] == "00123456"


def toss_token_handler(tokens, accepted, paths):
    def handle(request):
        paths.append(request.url.path)
        if request.url.path == "/oauth2/token":
            return httpx.Response(200, json={"access_token": next(tokens), "expires_in": 3600})
        if request.headers["Authorization"] != f"Bearer {accepted}":
            return httpx.Response(401, json={"error": {"code": "token-revoked"}})
        return httpx.Response(200, json={"result": [{"accountSeq": 7, "accountType": "BROKERAGE"}]})
    return handle


def test_toss_reissues_token_once_when_cached_token_is_revoked(settings):
    redis, paths = MemoryRedis(), []

    async def read():
        handler = toss_token_handler(iter(["stale", "fresh"]), "fresh", paths)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            broker = BrokerConnector(settings, redis, http, account=toss_account(TossSettings("id", "secret")))
            first = await broker.account._get("/api/v1/accounts")
            second = await broker.account._get("/api/v1/accounts")
            return first, second

    first, second = asyncio.run(read())
    assert first == second == [{"accountSeq": 7, "accountType": "BROKERAGE"}]
    assert paths == ["/oauth2/token", "/api/v1/accounts", "/oauth2/token", "/api/v1/accounts", "/api/v1/accounts"]
    cached, = redis.values.values()
    assert json.loads(cached[0])["access_token"] == "fresh"


def test_toss_reports_access_denied_when_reissued_token_is_also_rejected(settings):
    paths = []

    async def read():
        handler = toss_token_handler(iter(["stale", "also-stale"]), "never", paths)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            broker = BrokerConnector(settings, MemoryRedis(), http, account=toss_account(TossSettings("id", "secret")))
            await broker.account._get("/api/v1/accounts")

    with pytest.raises(BrokerConfigurationError, match="toss_access_denied"):
        asyncio.run(read())
    assert paths == ["/oauth2/token", "/api/v1/accounts", "/oauth2/token", "/api/v1/accounts"]


def test_toss_requires_account_selection_when_multiple_accounts_are_linked(settings):
    broker = BrokerConnector(settings, MemoryRedis(), None, account=toss_account(TossSettings("id", "secret")))
    assert isinstance(broker.account, TossAccount)
    broker.account._get = AsyncMock(return_value=[
        {"accountSeq": seq, "accountType": "BROKERAGE"} for seq in (2, 7)
    ])
    with pytest.raises(BrokerConfigurationError, match="toss_account_selection_required"):
        asyncio.run(broker.get_balance())
    broker.account._get.assert_awaited_once_with("/api/v1/accounts")

    configured = BrokerConnector(settings, MemoryRedis(), None,
                                 account=toss_account(TossSettings("id", "secret", "7")))
    configured.account._get = AsyncMock(side_effect=[
        broker.account._get.return_value, {"cashBuyingPower": "42"},
    ])
    assert asyncio.run(configured.get_orderable_cash()) == Decimal(42)
    assert configured.account._get.await_args.kwargs == {"account_seq": "7", "params": {"currency": "KRW"}}


@pytest.mark.parametrize("is_virtual", [True, False])
def test_account_domain_and_market_domains(settings, monkeypatch, is_virtual):
    monkeypatch.setenv("IS_VIRTUAL", str(is_virtual).lower())
    settings = BrokerSettings.from_env()
    request_log = []
    master = AsyncMock(return_value={
        "005930": stock("005930", "STOCK", "00123456"),
        "005935": stock("005935", "STOCK", "00123456"),
        "069500": stock("069500", "ETF", None),
        "580001": stock("580001", "ETN", None),
    })

    async def handle(request):
        domain = f"{request.url.scheme}://{request.url.netloc.decode()}"
        endpoint = request.url.path.rsplit("/", 1)[-1]
        request_log.append((endpoint, domain))
        if endpoint == "tokenP":
            payload = json.loads(request.content)
            assert payload["appkey"] == ("vts-key" if domain == VIRTUAL else "live-key")
            return httpx.Response(200, json={"access_token": domain, "expires_in": 3600})
        assert request.headers["Authorization"] == f"Bearer {domain}"
        if endpoint == "inquire-balance":
            assert request.url.params["CANO"] == ("87654321" if is_virtual else "12345678")
            assert request.url.params["ACNT_PRDT_CD"] == ("02" if is_virtual else "01")
            assert request.headers["tr_id"] == ("VTTC8434R" if is_virtual else "TTTC8434R")
            if not request.headers["tr_cont"]:
                assert request.url.params["CTX_AREA_NK100"] == ""
                return httpx.Response(200, headers={"tr_cont": "M"}, json={
                    "output1": [BALANCE_ROW | {"pdno": code} for code in ("005930", "005935")],
                    "output2": [{"dnca_tot_amt": "100.00", "tot_evlu_amt": "250.00"}],
                    "ctx_area_fk100": " first ", "ctx_area_nk100": " next ",
                })
            assert request.headers["tr_cont"] == "N"
            assert request.url.params["CTX_AREA_NK100"] == "next"
            return httpx.Response(200, json={
                "output1": [BALANCE_ROW | {"pdno": code} for code in ("069500", "Q580001")],
            })
        if endpoint == "inquire-price":
            assert request.url.params["FID_INPUT_ISCD"] == "Q580001"
            return httpx.Response(200, content=b'{"output":{"stck_prpr":123.123456789123456789}}')
        if endpoint == "inquire-daily-itemchartprice":
            return httpx.Response(200, json={"output2": [
                {"stck_bsop_date": "20261012", "stck_oprc": "100", "stck_hgpr": "130",
                 "stck_lwpr": "99", "stck_clpr": "123.45", "acml_vol": "1,234"},
            ]})
        assert endpoint == "inquire-investor"
        return httpx.Response(200, json={"output": market_rows()})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
            broker = BrokerConnector(settings, MemoryRedis(), http, stock_master=master)
            balance = await broker.get_balance()
            quote = await broker.get_quote("580001", asset_type="ETN")
            bars = await broker.get_daily_bars("005930", MONDAY, MONDAY)
            flow = await broker.get_five_day_flow("005930", as_of=datetime(2026, 10, 12, 16, tzinfo=KST))
            return balance, quote, bars, flow

    balance, quote, (bar,), flow = asyncio.run(run())
    market_endpoints = {"inquire-price", "inquire-daily-itemchartprice", "inquire-investor"}
    assert {endpoint for endpoint, _ in request_log if endpoint in market_endpoints} == market_endpoints
    assert all(domain == LIVE for endpoint, domain in request_log if endpoint in market_endpoints)
    assert {domain for endpoint, domain in request_log if endpoint == "inquire-balance"} == {
        VIRTUAL if is_virtual else LIVE
    }
    assert [holding.stock_code for holding in balance.holdings] == ["005930", "005935", "069500", "580001"]
    assert [holding.asset_type for holding in balance.holdings] == ["STOCK", "STOCK", "ETF", "ETN"]
    assert balance.holdings[0].corp_code == balance.holdings[1].corp_code == "00123456"
    master.assert_awaited_once_with(["005930", "005935", "069500", "580001"])
    assert balance.cash_weight == Decimal("0.4")
    assert balance.holdings[0].weight == Decimal("0.15")
    assert balance.holdings[0].average_price == Decimal("10.123456789123456789")
    assert quote.current_price == Decimal("123.123456789123456789")
    assert bar.volume == Decimal(1234)
    for record in [balance, *balance.holdings, quote, bar, flow, *flow.days]:
        for field in fields(record):
            if field.type is Decimal:
                assert isinstance(getattr(record, field.name), Decimal), field.name


def test_oauth_singleton_reuses_token_across_instances_and_refreshes_once(settings):
    issued = 0
    now = datetime(2026, 10, 12, tzinfo=KST)
    redis = MemoryRedis()

    async def handle(request):
        nonlocal issued
        if request.url.path.endswith("tokenP"):
            issued += 1
            await asyncio.sleep(0)
            return httpx.Response(200, json={"access_token": f"token-{issued}", "expires_in": 3600})
        return httpx.Response(200, json={"output": {"stck_prpr": "1"}})

    async def run():
        nonlocal now
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
            first, second = (BrokerConnector(settings, redis, http, clock=lambda: now) for _ in range(2))
            await asyncio.gather(first.get_quotes(["A", "B"]), second.get_quotes(["C", "D"]))
            assert issued == 1
            [(raw, ttl)] = redis.values.values()
            assert ttl == 3600
            assert json.loads(raw) == {
                "access_token": "token-1", "expires_at": (now + timedelta(hours=1)).isoformat(),
            }
            restarted = BrokerConnector(settings, redis, http, clock=lambda: now)
            await restarted.get_quote("E")
            assert issued == 1
            now += timedelta(minutes=59, seconds=1)
            await asyncio.gather(restarted.get_quotes(["A", "B"]), second.get_quote("C"))
            assert issued == 2

    asyncio.run(run())


def test_parallel_requests_are_limited_and_back_off_on_both_rate_limit_formats(settings):
    active = peak = 0
    quotes = {}
    delays = []

    async def handle(request):
        nonlocal active, peak
        if request.url.path.endswith("tokenP"):
            return httpx.Response(200, json={"access_token": "token", "expires_in": 3600})
        code = request.url.params["FID_INPUT_ISCD"]
        quotes[code] = quotes.get(code, 0) + 1
        active += 1
        peak = max(peak, active)
        try:
            await asyncio.sleep(0)
            if code == "005930" and quotes[code] == 1:
                return httpx.Response(429)
            if code == "005930" and quotes[code] == 2:
                return httpx.Response(500, json={"rt_cd": "1", "msg_cd": "EGW00201"})
            if code == "005930" and quotes[code] == 3:
                return httpx.Response(200, json={"rt_cd": "1", "msg_cd": "EGW00201"})
            return httpx.Response(200, json={"output": {"stck_prpr": "42"}})
        finally:
            active -= 1

    async def sleep(delay):
        delays.append(delay)
        await asyncio.sleep(0)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
            broker = BrokerConnector(settings, MemoryRedis(), http, sleep=sleep)
            return await broker.get_quotes(["005930", "005935", "069500", "000660"])

    assert len(asyncio.run(run())) == 4
    assert peak == 2
    assert delays == [0.25, 0.5, 1]
    assert quotes == {"005930": 4, "005935": 1, "069500": 1, "000660": 1}


@pytest.mark.parametrize(("as_of", "expected", "net_buy"), [
    (datetime(2026, 10, 12, 6, 39, 59, tzinfo=timezone.utc), FLOW_SESSIONS[:5], 15),
    (datetime(2026, 10, 12, 6, 40, tzinfo=timezone.utc), FLOW_SESSIONS[1:], 20),
    (datetime(2026, 10, 12, 6, 41, tzinfo=timezone.utc), FLOW_SESSIONS[1:], 20),
    (datetime(2026, 10, 9, 16, tzinfo=KST), FLOW_SESSIONS[:5], 15),
])
def test_five_confirmed_krx_sessions_at_cutoff_and_holiday(settings, as_of, expected, net_buy):
    async def handle(request):
        if request.url.path.endswith("tokenP"):
            return httpx.Response(200, json={"access_token": "token", "expires_in": 3600})
        return httpx.Response(200, json={"output": market_rows()})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
            broker = BrokerConnector(settings, MemoryRedis(), http, market_data=object())
            return await broker.get_five_day_flow("005930", as_of=as_of)

    result = asyncio.run(run())
    assert [row.session for row in result.days] == expected
    assert result.as_of == expected[-1]
    assert result.foreign_net_buy == Decimal(net_buy)
    assert result.institution_net_buy == Decimal(-net_buy)


def test_pykrx_fallback_for_all_market_endpoints(settings, monkeypatch):
    from pykrx import stock

    calls = []

    def ohlcv(start, end, ticker, *, adjusted):
        assert not adjusted
        calls.append(("ohlcv", start, end, ticker))
        return pd.DataFrame({
            "시가": [100], "고가": [133], "저가": [99], "종가": [123], "거래량": [1234],
        }, index=pd.to_datetime([MONDAY]))

    def flow(start, end, ticker):
        calls.append(("flow", start, end, ticker))
        return pd.DataFrame({
            "외국인합계": [4] * 6, "기관합계": [-2] * 6,
        }, index=pd.to_datetime(FLOW_SESSIONS))

    monkeypatch.setattr(stock, "get_market_ohlcv_by_date", ohlcv)
    monkeypatch.setattr(stock, "get_market_trading_volume_by_date", flow)
    retries = AsyncMock()

    async def handle(request):
        if request.url.path.endswith("tokenP"):
            return httpx.Response(200, json={"access_token": "token", "expires_in": 3600})
        if request.url.path.endswith("inquire-price"):
            return httpx.Response(429)
        if request.url.path.endswith("inquire-investor"):
            return httpx.Response(200, json={"output": market_rows()[1:]})
        return httpx.Response(503)

    async def run():
        now = datetime(2026, 10, 12, 16, tzinfo=KST)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
            broker = BrokerConnector(settings, MemoryRedis(), http, sleep=retries, clock=lambda: now)
            return (await broker.get_quote("005930"), await broker.get_daily_bars("005930", MONDAY, MONDAY),
                    await broker.get_five_day_flow("005930"))

    quote, (bar,), investor_flow = asyncio.run(run())
    assert retries.await_count == 6
    assert quote.source == "pykrx"
    assert quote.session == MONDAY
    assert bar.close == quote.current_price == Decimal(123)
    assert investor_flow.foreign_net_buy == Decimal(20)
    assert investor_flow.institution_net_buy == Decimal(-10)
    assert calls == [
        ("ohlcv", "20260928", "20261012", "005930"),
        ("ohlcv", "20261012", "20261012", "005930"),
        ("flow", "20261002", "20261012", "005930"),
    ]


def test_missing_flow_session_is_not_replaced_by_older_data(settings, monkeypatch):
    from pykrx import stock

    monkeypatch.setattr(stock, "get_market_trading_volume_by_date", lambda *args: pd.DataFrame())

    async def handle(request):
        if request.url.path.endswith("tokenP"):
            return httpx.Response(200, json={"access_token": "token", "expires_in": 3600})
        return httpx.Response(200, json={"output": market_rows()[1:]})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
            broker = BrokerConnector(settings, MemoryRedis(), http)
            await broker.get_five_day_flow("005930", as_of=datetime(2026, 10, 12, 16, tzinfo=KST))

    with pytest.raises(BrokerError) as caught:
        asyncio.run(run())
    assert "Five confirmed KRX sessions are not available" in str(caught.value.__cause__)


@pytest.mark.parametrize("provider,account_type", [("toss", TossAccount), ("kis", KisAccount)])
def test_health_and_connector_lifespan(settings, monkeypatch, provider, account_type):
    monkeypatch.setenv("BROKER_PROVIDER", provider)
    from backend import main
    warm = AsyncMock()
    monkeypatch.setattr(main.dart_rag, "warm_up", warm)

    async def run():
        async with app.router.lifespan_context(app), httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get("/health")
            assert response.json() == {"status": "ok"}
            assert app.state.broker.settings == settings
            # Either account source shares the one connector for KRX market data.
            assert type(app.state.broker.account) is account_type
            assert app.state.broker.account.connector is app.state.broker
            await asyncio.sleep(0)
            warm.assert_awaited_once()  # RAG and Kiwi models load in the background at startup

    asyncio.run(run())
