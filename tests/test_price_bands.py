"""Characterization tests for the one-month price band stream and the candle endpoint."""

import asyncio
from datetime import date, datetime, timedelta
from decimal import Decimal as D
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, create_autospec

import httpx
import pytest

from backend import dart_rag, llm, price_bands
from backend.broker import KST, BrokerConnector, Candle, DailyBar, FiveDayFlow, Holding, InvestorFlow, Quote
from backend.dart_etl import DartETL
from backend.main import app, price_band_events
from backend.sync_stock_master import Stock


NOW = datetime(2026, 11, 20, 16, tzinfo=KST)
RECEIPT = "20261110000001"
STOCK = Stock("005930", "00126380", "KR7005930003", None, "삼성전자", "STOCK", "264", D(5969782550))


def bars(day=NOW.date()):
    return tuple(DailyBar(day - timedelta(days=i), D(100), D(110), D(90), D(100), D(10)) for i in reversed(range(20)))


def flow(foreign=D(5), institution=D(10)):
    days = tuple(InvestorFlow(NOW.date() - timedelta(days=i), D(1), D(2)) for i in reversed(range(5)))
    return FiveDayFlow(days, foreign, institution)


def holding(code, weight, *, corp_code=None):
    return Holding(code, "종목" + code, "STOCK", corp_code, None, D(1), D(100), D(105),
                   D(100), D(105), D(5), D(5), D(weight))


def parse(frames):
    events = []
    for frame in frames:
        event, data = frame.strip().splitlines()
        events.append((event.removeprefix("event: "), json.loads(data.removeprefix("data: "))))
    return events


async def collect(stream):
    return [frame async for frame in stream]


def market(now=NOW):
    broker = create_autospec(BrokerConnector, instance=True)
    broker.clock = lambda: now
    broker.get_quote.return_value = Quote("005930", D(105), "kis", change_pct=D("1.5"))
    broker.get_daily_bars.return_value = bars()
    broker.get_five_day_flow.return_value = flow()
    etl = create_autospec(DartETL, instance=True)
    etl.has_recent_chunks.return_value = True
    return SimpleNamespace(broker=broker, etl=etl)


def test_market_band_levels_from_recent_range_and_flow():
    band, signals = price_bands.calculate_market_band("005930", "삼성전자", D(105), bars(), flow())
    levels = {key: (value["price"], value["current_pct"]) for key, value in band["levels"].items()}
    assert levels == {"buy": (D(85), D("-19.05")), "sell": (D(135), D("28.57")), "ceiling": (D(145), D("38.10"))}
    assert band["position_pct"] == D("40.00") and band["ma20"] == D(100) and band["indicative"] is False
    assert band["recent_range"] == {"sessions": 20, "low": D(90), "high": D(110)}
    assert signals["scores"] == {"ma20": 1, "foreign": 1, "institution": 1}


def test_market_band_without_history_is_indicative():
    band, signals = price_bands.calculate_market_band("005930", "삼성전자", D(100), (), None)
    assert [band["levels"][key]["price"] for key in price_bands.LEVELS] == [D(95), D(107), D(110)]
    assert band["indicative"] is True and band["position_pct"] == D("41.67")
    assert band["ma20"] is None and band["recent_range"] is None and band["flow"] is None
    assert signals["scores"] == {"ma20": None, "foreign": None, "institution": None}


def test_target_stream_sends_local_band_then_cited_disclosure(monkeypatch):
    monkeypatch.setattr(price_bands, "fetch_stocks", AsyncMock(return_value={"005930": STOCK}))
    monkeypatch.setattr(dart_rag, "search", AsyncMock(return_value=[
        {"rcept_no": RECEIPT, "report_nm": f"실적 공시 {RECEIPT}", "chunk_text": "영업이익이 늘었다."}]))
    answer = price_bands.BandAnalysis(analysis_text="요약", citations=["DOC-1"], reasons=[price_bands.Reasons(
        name="삼성전자", buy="[DOC-1] 실적 개선 공시가 지지 근거입니다.", sell="", ceiling="")])
    invoke = AsyncMock(return_value=answer)
    monkeypatch.setattr(llm, "invoke", invoke)
    data = market()

    events = parse(asyncio.run(collect(price_band_events(data, "005930"))))
    assert [event for event, _ in events] == ["status", "price_bands", "done"]
    preliminary, final = events[1][1], events[2][1]
    assert final["ok"] is True and final["mode"] == "price_bands"
    assert preliminary["as_of"] == final["as_of"] == "2026-11-20T16:00:00+09:00"
    assert set(final["band"]["levels"]) == {"buy_zone", "sell_zone", "peak_limit"}
    assert final["band"]["change_pct"] == "1.5"
    assert preliminary["band"]["disclosures"] == []
    assert final["band"]["disclosures"] == [
        {"alias": "DOC-1", "title": "실적 공시 (공시번호 생략)", "receipt": RECEIPT}]
    assert final["band"]["levels"]["buy_zone"]["reason"] == "[DOC-1] 실적 개선 공시가 지지 근거입니다."
    assert final["band"]["levels"]["sell_zone"] == preliminary["band"]["levels"]["sell_zone"]
    # The cloud prompt carries masked disclosures, not receipts.
    assert RECEIPT not in invoke.await_args.args[2]
    data.etl.ingest_missing_stock.assert_awaited_once_with(
        "005930", "00126380", NOW.date() - timedelta(days=60), NOW.date())


@pytest.mark.parametrize("now,end", [
    (NOW, date(2026, 11, 20)),
    (NOW.replace(hour=15, minute=39, second=59), date(2026, 11, 19)),
    (NOW.replace(hour=15, minute=40), date(2026, 11, 20)),
])
def test_daily_bars_end_on_the_last_confirmed_session(monkeypatch, now, end):
    monkeypatch.setattr(price_bands, "fetch_stocks", AsyncMock(return_value={"005930": STOCK}))
    monkeypatch.setattr(llm, "invoke", AsyncMock(side_effect=llm.LLMUnavailable("offline")))
    monkeypatch.setattr(dart_rag, "search", AsyncMock(return_value=[]))
    data = market(now)
    asyncio.run(collect(price_band_events(data, "005930")))
    data.broker.get_daily_bars.assert_awaited_once_with(
        "005930", end - timedelta(days=60), end, asset_type="STOCK")


def test_unknown_or_unpriced_stock_ends_with_error(monkeypatch):
    monkeypatch.setattr(price_bands, "fetch_stocks", AsyncMock(return_value={}))
    events = parse(asyncio.run(collect(price_band_events(market(), "999999"))))
    assert events[1:] == [("error", {"message": "등록된 종목을 찾을 수 없습니다."}), ("done", {"ok": False})]

    monkeypatch.setattr(price_bands, "fetch_stocks", AsyncMock(return_value={"005930": STOCK}))
    data = market()
    data.broker.get_quote.side_effect = RuntimeError("down")
    data.broker.get_daily_bars.return_value = ()
    events = parse(asyncio.run(collect(price_band_events(data, "005930"))))
    assert events[1][0] == "error" and "현재가와 최근 종가" in events[1][1]["message"]


def test_portfolio_stream_orders_holdings_by_weight(monkeypatch):
    monkeypatch.setattr(llm, "invoke", AsyncMock(side_effect=llm.LLMUnavailable("offline")))
    monkeypatch.setattr(dart_rag, "search", AsyncMock(return_value=[]))
    data = market()
    data.broker.get_balance.return_value = SimpleNamespace(holdings=(
        holding("000002", "0.2"), holding("000001", "0.5", corp_code="00000001")))
    events = parse(asyncio.run(collect(price_band_events(data))))
    assert [event for event, _ in events] == ["status", "price_bands", "done"]
    bands = events[2][1]["bands"]
    assert [band["stock_code"] for band in bands] == ["000001", "000002"]
    assert set(bands[0]["levels"]) == {"buy", "sell", "ceiling"}
    assert bands[0]["average_price"] == "100" and "average_pct" in bands[0]["levels"]["sell"]
    data.etl.ingest_stock.assert_awaited_once()


class MemoryRedis:
    def __init__(self):
        self.values = {}

    async def get(self, key):
        return self.values.get(key, (None, None))[0]

    async def set(self, key, value, *, ex):
        self.values[key] = (value, ex)


def candle_broker():
    broker = SimpleNamespace(redis=MemoryRedis(), clock=lambda: NOW)
    broker.get_ten_minute_candles = AsyncMock(return_value=[Candle(1763600400, D(1), D(2), D(1), D(2), D(3))])
    broker.get_candle_history = AsyncMock(return_value=[Candle(date(2026, 11, 20), D(1), D(2), D(1), D(2), D(3))])
    return broker


def request_candles(path):
    async def get():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://local") as client:
            return await client.get(path)

    return asyncio.run(get())


def test_candles_are_cached_per_interval_with_poll_hint(monkeypatch):
    broker = candle_broker()
    monkeypatch.setattr(app.state, "broker", broker, raising=False)
    first = request_candles("/api/v1/stocks/005930/candles")
    second = request_candles("/api/v1/stocks/005930/candles")
    assert first.status_code == second.status_code == 200
    assert first.json() == [{"time": 1763600400, "open": "1", "high": "2", "low": "1", "close": "2", "volume": "3"}]
    assert first.headers["X-Candle-Poll-After"] == second.headers["X-Candle-Poll-After"]
    broker.get_ten_minute_candles.assert_awaited_once_with("005930")
    assert broker.redis.values["finguard:candles:v2:10m:005930"][1] == 15

    weekly = request_candles("/api/v1/stocks/005930/candles?interval=1w")
    assert weekly.json()[0]["time"] == "2026-11-20" and "X-Candle-Poll-After" not in weekly.headers
    broker.get_candle_history.assert_awaited_once_with("005930", "W")
    assert broker.redis.values["finguard:candles:v2:1w:005930"][1] == 3600


def test_empty_candles_are_not_cached(monkeypatch):
    broker = candle_broker()
    broker.get_candle_history.return_value = []
    monkeypatch.setattr(app.state, "broker", broker, raising=False)
    for _ in range(2):
        response = request_candles("/api/v1/stocks/005930/candles?interval=1d")
        assert response.status_code == 502
    assert broker.get_candle_history.await_count == 2 and broker.redis.values == {}
