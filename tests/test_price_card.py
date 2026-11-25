"""Analyst price card: local snapshot first, validated AI card second, Redis cache reuse."""

import asyncio
from datetime import date, datetime, timedelta
from decimal import Decimal as D
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend import dart_rag, llm, price_card
from backend.broker import KST, DailyBar, Holding, Quote
from backend.card_validation import CardAnalysis, LevelProposal, Risk
from backend.main import price_band_events
from backend.price_card import card_ttl, card_updates, get_or_build_card
from backend.serialization import dump
from backend.sync_stock_master import Stock

from test_price_bands import NOW, RECEIPT, STOCK, collect, market, parse


END = date(2026, 11, 20)
CURRENT = D(10050)
ETF = Stock("069500", None, "KR7069500007", None, "KODEX 200", "ETF", None, D(1))
NEWS = [{"title": f"삼성전자 기사 {i}", "description": f"요약 {i}", "link": f"https://news.example/{i}",
         "published_at": datetime(2026, 11, 19, 9, tzinfo=KST)} for i in range(1, 4)]


def zigzag(count=85):
    """Closes cycle 9,600..10,300 so swings, volume zones and ATR14 all exist."""
    rows = []
    for i in range(count):
        close = D(10000) + D(100) * ((i % 8) - 4)
        rows.append(DailyBar(END - timedelta(days=count - 1 - i), close, close + 80, close - 80, close,
                             D(1000 + 10 * i)))
    return tuple(rows)


def card_market(stock=STOCK, now=NOW):
    data = market(now)
    data.broker.get_quote.return_value = Quote(stock.stock_code, CURRENT, "kis", change_pct=D("0.5"))
    data.broker.get_daily_bars.return_value = zigzag()
    data.external.get_news.return_value = NEWS
    return data


def facts_of(context):
    return {fact["label"]: fact for fact in json.loads(context)["facts"]}


def analyst(low_reason="[P-1] 현재가 아래 지지 후보입니다 [NEWS-2].", *, offsets=(103, 101, 200), summary=""):
    async def invoke(role, instruction, context, schema, *, timeout):
        assert role == "analyst" and schema is CardAnalysis and timeout == price_card.ANALYST_TIMEOUT
        current = D(facts_of(context)["현재가"]["value"])
        down, up, top = offsets
        return CardAnalysis(
            low=LevelProposal(price=str(current - down), basis=["P-1"], reason=low_reason),
            high=LevelProposal(price=str(current + up), basis=["P-1"], reason="[P-1] 위 단기 고점 후보입니다."),
            ceiling=LevelProposal(price=str(current + top), basis=["P-1"], reason="[P-1] 위 10거래일 상단입니다."),
            summary=summary, risks=[Risk(text="실적 발표 예정 [NEWS-3]", basis=["NEWS-3"])], citations=[])
    return AsyncMock(side_effect=invoke)


def patch(monkeypatch, invoke, stock=STOCK, chunks=()):
    monkeypatch.setattr(price_card, "fetch_stocks", AsyncMock(return_value={stock.stock_code: stock}))
    search = AsyncMock(return_value=list(chunks))
    monkeypatch.setattr(dart_rag, "search", search)
    monkeypatch.setattr(llm, "invoke", invoke)
    return search


def sources(snapshot):
    return {key: level["source"] for key, level in snapshot["band"]["levels"].items()}


def test_card_streams_local_then_validated_ai_card(monkeypatch):
    invoke = analyst(summary="향후 2주간 [P-1] 부근 등락이 예상됩니다.")
    patch(monkeypatch, invoke, chunks=[
        {"rcept_no": RECEIPT, "report_nm": "분기보고서", "chunk_text": "매출이 늘었다."}])
    data = card_market()

    events = parse(asyncio.run(collect(price_band_events(data, "005930"))))
    assert [event for event, _ in events] == ["status", "price_bands", "done"]
    preliminary, final = events[1][1]["band"], events[2][1]["band"]
    assert events[2][1]["ok"] is True

    assert set(preliminary["levels"]) == set(final["levels"]) == {"buy_zone", "sell_zone", "peak_limit"}
    assert {level["source"] for level in preliminary["levels"].values()} == {"local"}
    assert preliminary["ai_status"] == "local" and preliminary["horizon_sessions"] == 10
    assert final["ai_status"] == "ai" and final["horizon_sessions"] == 10
    # STOCK tick for 10,000~20,000 won is 10 won.
    assert {key: level["price"] for key, level in final["levels"].items()} == {
        "buy_zone": "9950", "sell_zone": "10150", "peak_limit": "10250"}
    assert final["levels"]["buy_zone"]["basis"] == ["P-1"]
    assert final["levels"]["buy_zone"]["current_pct"] == "-1.00"
    assert final["summary"] == "향후 2주간 [P-1] 부근 등락이 예상됩니다."
    assert final["risks"] == [{"text": "실적 발표 예정 [NEWS-3]", "basis": ["NEWS-3"]}]
    # Only cited news are listed; DOC-1 was offered but never cited.
    assert [row["alias"] for row in final["news"]] == ["NEWS-2", "NEWS-3"]
    assert final["news"][0] == {"alias": "NEWS-2", "title": "삼성전자 기사 2", "link": "https://news.example/2"}
    assert final["disclosures"] == [] and final["warnings"] == []
    for key in ("change_pct", "position_pct", "indicative", "ma20", "recent_range", "flow"):
        assert key in final, key

    # Price-like facts reach the prompt and the snapshot as whole won.
    context = invoke.await_args.args[2]
    prompt_facts = json.loads(context)["facts"]
    assert final["facts"] == prompt_facts
    labels = [fact["label"] for fact in prompt_facts]
    assert {"현재가", "5일선", "20일선", "60일선", "스윙 저점", "스윙 고점", "매물대", "ATR14"} <= set(labels)
    for fact in prompt_facts:
        for value in (fact["value"], fact["upper"]):
            assert value is None or "." not in value, fact
    assert "10거래일" in invoke.await_args.args[1]
    assert [row["id"] for row in json.loads(context)["news"]] == ["NEWS-1", "NEWS-2", "NEWS-3"]
    assert [row["id"] for row in json.loads(context)["docs"]] == ["DOC-1"]
    assert "holding" not in json.loads(context)

    start, end = data.broker.get_daily_bars.await_args.args[1:]
    assert end == END and start == end - timedelta(days=120)
    data.external.get_news.assert_awaited_once_with("삼성전자", limit=10)


def test_partial_card_when_one_level_fails(monkeypatch):
    patch(monkeypatch, analyst(low_reason="[NEWS-9] 없는 기사"))
    snapshots = asyncio.run(collect(card_updates(card_market(), "005930")))
    final = snapshots[-1]["band"]
    assert final["ai_status"] == "partial"
    assert sources(snapshots[-1]) == {"buy_zone": "local", "sell_zone": "ai", "peak_limit": "ai"}
    assert final["warnings"] == ["AI 단기 저점 검증 실패로 기본 계산을 사용했습니다."]


def test_analyst_outage_still_finishes_with_local_card(monkeypatch):
    patch(monkeypatch, AsyncMock(side_effect=llm.LLMUnavailable("offline")))
    data = card_market()
    events = parse(asyncio.run(collect(price_band_events(data, "005930"))))
    assert [event for event, _ in events] == ["status", "price_bands", "done"]
    final = events[2][1]
    assert final["ok"] is True
    assert final["band"]["ai_status"] == "local"
    assert {level["source"] for level in final["band"]["levels"].values()} == {"local"}
    assert len(final["band"]["warnings"]) >= 1
    assert final["band"]["levels"] == events[1][1]["band"]["levels"]


BAND_KEYS = {"stock_code", "name", "current_price", "change_pct", "position_pct", "indicative", "ma20",
             "recent_range", "flow", "levels", "disclosures", "horizon_sessions", "summary", "risks", "news",
             "facts", "ai_status", "warnings"}


def test_local_card_carries_every_retrieved_material(monkeypatch):
    other = "20261102000123"
    patch(monkeypatch, AsyncMock(side_effect=llm.LLMUnavailable("offline")), chunks=[
        {"rcept_no": RECEIPT, "report_nm": f"분기보고서 {RECEIPT}", "chunk_text": f"매출이 늘었다. 접수 {RECEIPT}."},
        {"rcept_no": other, "report_nm": "전환사채 발행결정", "chunk_text": "전환가액은 9,800원이다."},
        {"rcept_no": RECEIPT, "report_nm": "분기보고서", "chunk_text": "영업이익이 줄었다."}])
    snapshots = asyncio.run(collect(card_updates(card_market(), "005930")))
    preliminary, final = (snapshot["band"] for snapshot in snapshots)
    # The local card goes out before news and DART are read.
    assert preliminary["materials"] == {"docs": [], "news": []}
    assert final["ai_status"] == "local"
    # Every retrieved chunk, in search order, masked as AliasBook.document produces; one alias per filing.
    masked = "(공시번호 생략)"
    assert final["materials"]["docs"] == [
        {"alias": "DOC-1", "title": f"분기보고서 {masked}", "text": f"매출이 늘었다. 접수 {masked}.", "receipt": RECEIPT},
        {"alias": "DOC-2", "title": "전환사채 발행결정", "text": "전환가액은 9,800원이다.", "receipt": other},
        {"alias": "DOC-1", "title": "분기보고서", "text": "영업이익이 줄었다.", "receipt": RECEIPT}]
    assert final["materials"]["news"] == [
        {"alias": f"NEWS-{i}", "title": f"삼성전자 기사 {i}", "summary": f"요약 {i}",
         "published_at": "2026-11-19T09:00:00+09:00", "link": f"https://news.example/{i}"} for i in range(1, 4)]
    # The UI-facing card keeps its shape: only cited filings and articles, which a local card has none of.
    assert final["disclosures"] == [] and final["news"] == []
    assert set(final) == set(preliminary) == BAND_KEYS | {"materials"}


def test_cache_hit_skips_analyst_and_yields_once(monkeypatch):
    invoke = analyst()
    patch(monkeypatch, invoke)
    store = {}
    data = card_market()
    data.broker.redis = SimpleNamespace(get=AsyncMock(side_effect=store.get),
                                        set=AsyncMock(side_effect=lambda k, v, ex: store.update({k: v})))

    first = asyncio.run(collect(card_updates(data, "005930")))
    assert len(first) == 2
    key = "finguard:price-card:v1:005930:2026-11-20"
    data.broker.redis.set.assert_awaited_once_with(key, dump(first[-1]), ex=card_ttl(NOW))

    second = asyncio.run(collect(card_updates(data, "005930")))
    assert len(second) == 1
    assert invoke.await_count == 1
    assert dump(second[0]) == dump(first[-1])
    assert second[0] == first[-1]
    assert asyncio.run(get_or_build_card(data, "005930")) == first[-1]
    data.broker.get_daily_bars.assert_awaited_once()


def test_cache_failure_only_logs(monkeypatch):
    patch(monkeypatch, analyst())
    data = card_market()
    data.broker.redis = SimpleNamespace(get=AsyncMock(side_effect=ConnectionError("down")),
                                        set=AsyncMock(side_effect=ConnectionError("down")))
    snapshots = asyncio.run(collect(card_updates(data, "005930")))
    assert len(snapshots) == 2 and snapshots[-1]["band"]["ai_status"] == "ai"


def test_cache_key_and_ttl():
    assert price_card.CACHE_KEY.format(code="005930", session="2026-11-20") == "finguard:price-card:v1:005930:2026-11-20"
    assert card_ttl(datetime(2026, 11, 20, 10, tzinfo=KST)) == 600                          # 금 장중
    assert card_ttl(datetime(2026, 11, 19, 16, tzinfo=KST)) == 16 * 3600 + 30 * 60          # 목 마감 후 → 금 08:30
    assert card_ttl(datetime(2026, 11, 20, 16, tzinfo=KST)) == 64 * 3600 + 30 * 60          # 금 마감 후 → 월 08:30
    assert card_ttl(datetime(2026, 11, 23, 7, tzinfo=KST)) == 90 * 60                       # 월 개장 전 → 당일 08:30
    assert card_ttl(datetime(2026, 11, 22, 12, tzinfo=KST)) == 20 * 3600 + 30 * 60          # 일 → 월 08:30


def held(average="9876.4", weight="0.2512", profit="1.76"):
    return Holding("005930", "삼성전자", "STOCK", "00126380", None, D(10), D(average), CURRENT,
                   D(98764), D(100500), D(1736), D(profit), D(weight))


def test_holding_numbers_are_allowed_only_when_held(monkeypatch):
    reason = "[P-1] 아래이며 평단 9,876원과 비중 25.12%를 고려한 지지 후보입니다."

    invoke = analyst(low_reason=reason)
    patch(monkeypatch, invoke)
    data = card_market()
    data.broker.get_balance.return_value = SimpleNamespace(holdings=(held(),))
    final = asyncio.run(get_or_build_card(data, "005930"))["band"]
    assert final["levels"]["buy_zone"]["source"] == "ai"
    holding = json.loads(invoke.await_args.args[2])["holding"]
    assert holding == {"average_price": "9876", "weight_pct": "25.12", "profit_loss_pct": "1.76"}
    data.broker.get_balance.assert_awaited_once_with()

    for balance in (SimpleNamespace(holdings=()), RuntimeError("broker down")):
        invoke = analyst(low_reason=reason)
        patch(monkeypatch, invoke)
        data = card_market()
        if isinstance(balance, Exception):
            data.broker.get_balance.side_effect = balance
        else:
            data.broker.get_balance.return_value = balance
        final = asyncio.run(get_or_build_card(data, "005930"))["band"]
        assert final["levels"]["buy_zone"]["source"] == "local"
        assert "holding" not in json.loads(invoke.await_args.args[2])


def test_etf_skips_disclosures_and_uses_5_won_tick(monkeypatch):
    search = patch(monkeypatch, analyst(), stock=ETF)
    data = card_market(stock=ETF)
    snapshots = asyncio.run(collect(card_updates(data, "069500")))
    final = snapshots[-1]["band"]
    assert final["ai_status"] == "ai"
    # 10,050 - 103 = 9,947 -> 9,945 on the 5-won ETF tick (a stock would round to 9,950).
    assert {key: level["price"] for key, level in final["levels"].items()} == {
        "buy_zone": D(9945), "sell_zone": D(10150), "peak_limit": D(10250)}
    assert final["disclosures"] == [] and final["materials"]["docs"] == []
    search.assert_not_awaited()
    data.etl.ingest_missing_stock.assert_not_awaited()
    data.etl.has_recent_chunks.assert_not_awaited()
    data.broker.get_daily_bars.assert_awaited_once_with(
        "069500", END - timedelta(days=120), END, asset_type="ETF")


def test_disclosure_search_budget_covers_a_cold_model_load(monkeypatch):
    # Measured 2026-11-26 (RTX GPU, fresh process): Kiwi 1.8s + bge-m3/reranker load 16.3s + first encode
    # 1.0s = 19.1s cold; 0.6s warm. An 8s budget dropped every DART chunk from the first card after a start.
    patch(monkeypatch, analyst())

    async def search(*args, **kwargs):
        return []

    monkeypatch.setattr(dart_rag, "search", search)
    budgets = {}
    original = price_card.optional_data

    async def spy(operation, timeout, **kwargs):
        budgets[getattr(operation, "__name__", repr(operation))] = timeout
        return await original(operation, timeout, **kwargs)

    monkeypatch.setattr(price_card, "optional_data", spy)
    data = card_market()
    data.etl.has_recent_chunks.return_value = True
    asyncio.run(collect(card_updates(data, "005930")))

    search_budget = budgets["search"]
    assert search_budget >= 25
    # Reference inputs, the analyst and its single fallback still fit inside the whole-build cap.
    # 8s on-demand ingest + 3s chunk check run before the search (news runs beside them, capped at 8s).
    assert 8 + 3 + search_budget + price_card.ANALYST_TIMEOUT + llm.FALLBACK_TIMEOUT <= price_card.BUILD_TIMEOUT


def memory_redis(store):
    return SimpleNamespace(get=AsyncMock(side_effect=store.get),
                           set=AsyncMock(side_effect=lambda k, v, ex: store.update({k: v})))


def test_cache_hit_over_sse_still_ends_with_done(monkeypatch):
    invoke = analyst()
    patch(monkeypatch, invoke)
    data = card_market()
    data.broker.redis = memory_redis({})

    first = parse(asyncio.run(collect(price_band_events(data, "005930"))))
    second = parse(asyncio.run(collect(price_band_events(data, "005930"))))
    assert [event for event, _ in first] == ["status", "price_bands", "done"]
    assert [event for event, _ in second] == ["status", "price_bands", "done"]
    assert second[-1][1]["ok"] is True
    assert second[-1][1]["band"] == first[-1][1]["band"]
    assert invoke.await_count == 1


def test_concurrent_builds_of_one_card_call_the_analyst_once(monkeypatch):
    # Chat and the price-card screen can ask for the same card at the same moment.
    slow = analyst()
    build = slow.side_effect

    async def invoke(*args, **kwargs):
        await asyncio.sleep(0.05)  # the analyst call is where both requests would overlap
        return await build(*args, **kwargs)

    slow.side_effect = invoke
    patch(monkeypatch, slow)
    data = card_market()
    data.broker.redis = memory_redis({})

    async def both():
        sse = asyncio.create_task(collect(price_band_events(data, "005930")))
        chat = asyncio.create_task(get_or_build_card(data, "005930"))
        return await sse, await chat

    frames, card = asyncio.run(both())
    assert slow.await_count == 1
    assert [event for event, _ in parse(frames)] == ["status", "price_bands", "done"]
    # Whichever request waited got the other's cached card.
    assert json.loads(dump(card["band"])) == parse(frames)[-1][1]["band"]


def test_waiting_for_another_build_never_delays_the_local_snapshot(monkeypatch):
    slow = analyst()
    build = slow.side_effect

    async def scenario():
        release = asyncio.Event()

        async def invoke(*args, **kwargs):
            await release.wait()  # the first build holds the lock until the analyst answers
            return await build(*args, **kwargs)

        slow.side_effect = invoke
        first = card_updates(data, "005930")
        assert (await first.__anext__())["band"]["ai_status"] == "local"
        first_final = asyncio.create_task(first.__anext__())
        while not slow.await_count:
            await asyncio.sleep(0.01)

        second = card_updates(data, "005930")
        # (a) The second caller's local card arrives while the first build still holds the lock.
        second_local = await asyncio.wait_for(second.__anext__(), 1)
        assert second_local["band"]["ai_status"] == "local"
        second_final = asyncio.create_task(second.__anext__())
        await asyncio.sleep(0.05)
        assert not second_final.done()

        release.set()
        finals = await first_final, await second_final
        for updates in (first, second):
            with pytest.raises(StopAsyncIteration):
                await updates.__anext__()
        return finals

    patch(monkeypatch, slow)
    data = card_market()
    data.broker.redis = memory_redis({})
    first_final, second_final = asyncio.run(scenario())
    # (b) The waiting caller reads the first caller's cached card; the analyst ran once.
    assert second_final == first_final and first_final["band"]["ai_status"] == "ai"
    assert slow.await_count == 1


def test_local_only_card_is_cached_briefly_even_when_analyst_answered(monkeypatch):
    # Every level cites a missing tag, so all three fail validation.
    async def invoke(role, instruction, context, schema, *, timeout):
        bad = LevelProposal(price="10050", basis=["P-1"], reason="[NEWS-99] 없는 기사")
        return CardAnalysis(low=bad, high=bad, ceiling=bad, summary="", risks=[], citations=[])
    patch(monkeypatch, AsyncMock(side_effect=invoke))
    data = card_market()
    final = asyncio.run(get_or_build_card(data, "005930"))
    assert final["band"]["ai_status"] == "local"
    assert card_ttl(NOW) > 600
    assert data.broker.redis.set.await_args.kwargs["ex"] <= 600


def test_malformed_cache_entry_rebuilds_the_card(monkeypatch):
    invoke = analyst()
    patch(monkeypatch, invoke)
    data = card_market()
    broken = json.dumps({"as_of": "x", "band": {"levels": [], "facts": []}})
    data.broker.redis = memory_redis({"finguard:price-card:v1:005930:2026-11-20": broken})
    snapshots = asyncio.run(collect(card_updates(data, "005930")))
    assert len(snapshots) == 2 and snapshots[-1]["band"]["ai_status"] == "ai"
    assert invoke.await_count == 1


def test_card_validation_runs_off_the_event_loop_thread(monkeypatch):
    import threading

    patch(monkeypatch, analyst())
    seen = []
    validate = price_card.validate_card

    def spy(*args):
        seen.append(threading.current_thread() is threading.main_thread())
        return validate(*args)

    monkeypatch.setattr(price_card, "validate_card", spy)
    final = asyncio.run(get_or_build_card(card_market(), "005930"))
    assert final["band"]["ai_status"] == "ai" and seen == [False]


def test_build_survives_a_consumer_that_goes_away(monkeypatch):
    release = asyncio.Event()
    build = analyst().side_effect

    entered = asyncio.Event()

    async def slow(*args, **kwargs):
        entered.set()
        await release.wait()
        return await build(*args, **kwargs)

    invoke = AsyncMock(side_effect=slow)
    patch(monkeypatch, invoke)
    data = card_market()
    store = {}
    data.broker.redis = memory_redis(store)
    key = "finguard:price-card:v1:005930:2026-11-20"

    async def scenario():
        updates = card_updates(data, "005930")
        assert (await anext(updates))["band"]["ai_status"] == "local"
        consumer = asyncio.create_task(anext(updates))
        await asyncio.wait_for(entered.wait(), 5)  # the analyst is running
        consumer.cancel()  # chat gives up while the analyst is still running
        await asyncio.gather(consumer, return_exceptions=True)
        await updates.aclose()
        assert invoke.await_count == 1 and store == {} and key in price_card.card_builds
        release.set()  # the build finishes without any consumer and writes the cache
        for _ in range(200):
            if key in store and key not in price_card.card_builds:
                break
            await asyncio.sleep(0.01)
        assert key in store and key not in price_card.card_builds
        return await asyncio.wait_for(get_or_build_card(data, "005930"), 2)

    final = asyncio.run(scenario())
    assert final["band"]["ai_status"] == "ai" and invoke.await_count == 1


def test_two_consumers_share_one_build(monkeypatch):
    release = asyncio.Event()
    build = analyst().side_effect

    entered = asyncio.Event()

    async def slow(*args, **kwargs):
        entered.set()
        await release.wait()
        return await build(*args, **kwargs)

    invoke = AsyncMock(side_effect=slow)
    patch(monkeypatch, invoke)
    data = card_market()
    data.broker.redis = memory_redis({})

    async def scenario():
        first, second = card_updates(data, "005930"), card_updates(data, "005930")
        await first.__anext__(), await second.__anext__()
        a, b = asyncio.create_task(first.__anext__()), asyncio.create_task(second.__anext__())
        await asyncio.wait_for(entered.wait(), 5)
        await asyncio.sleep(0)
        assert not a.done() and not b.done()  # both wait on the one build
        release.set()
        return await a, await b

    a, b = asyncio.run(scenario())
    assert a == b and a["band"]["ai_status"] == "ai"
    assert invoke.await_count == 1


def test_failed_build_without_consumers_is_logged_and_removed(monkeypatch, caplog):
    import gc

    fail = asyncio.Event()
    started = asyncio.Event()

    async def boom(*args, **kwargs):
        started.set()
        await fail.wait()
        raise RuntimeError("build exploded")

    patch(monkeypatch, AsyncMock(side_effect=analyst().side_effect))
    monkeypatch.setattr(price_card, "built_card", boom)
    data = card_market()
    data.broker.redis = memory_redis({})
    key = "finguard:price-card:v1:005930:2026-11-20"
    reported = []

    async def scenario():
        asyncio.get_running_loop().set_exception_handler(lambda loop, context: reported.append(context))
        updates = card_updates(data, "005930")
        await anext(updates)
        consumer = asyncio.create_task(anext(updates))
        await asyncio.wait_for(started.wait(), 5)
        consumer.cancel()  # the only consumer goes away while the build is live
        await asyncio.gather(consumer, return_exceptions=True)
        await updates.aclose()
        assert key in price_card.card_builds
        fail.set()
        for _ in range(200):
            if key not in price_card.card_builds:
                break
            await asyncio.sleep(0.01)
        gc.collect()
        await asyncio.sleep(0.01)

    with caplog.at_level("WARNING", logger=price_card.logger.name):
        asyncio.run(scenario())
    assert key not in price_card.card_builds
    assert "Price card build failed (RuntimeError)" in caplog.text
    assert reported == []


def test_hung_build_is_capped_and_its_key_freed(monkeypatch, caplog):
    hang = asyncio.Event()

    async def never(*args, **kwargs):
        await hang.wait()

    patch(monkeypatch, analyst())
    monkeypatch.setattr(price_card, "built_card", never)
    monkeypatch.setattr(price_card, "BUILD_TIMEOUT", 0.05)
    data = card_market()
    data.broker.redis = memory_redis({})
    key = "finguard:price-card:v1:005930:2026-11-20"

    async def scenario():
        updates = card_updates(data, "005930")
        await anext(updates)
        with pytest.raises(TimeoutError):  # consumers see the failure like any other build error
            await asyncio.wait_for(anext(updates), 5)
        await asyncio.sleep(0)
        return key in price_card.card_builds

    with caplog.at_level("WARNING", logger=price_card.logger.name):
        assert asyncio.run(scenario()) is False
    assert "Price card build failed (TimeoutError)" in caplog.text
