"""Price-card log: scoring window, UPSERT, and the card_updates hook."""

import asyncio
from datetime import date, timedelta
from decimal import Decimal as D
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from psycopg.rows import dict_row

from backend import card_log, price_card
from backend.db import connection
from backend.broker import DailyBar
from backend.price_card import card_updates

from test_price_card import analyst, card_market, patch
from test_price_bands import collect


AS_OF = date(2026, 11, 20)
ROW = {"low": D(1000), "high": D(1100), "ceiling": D(1200),
       "local_low": D(990), "local_high": D(1090), "local_ceiling": D(1150)}
RESULT_KEYS = {"low_held", "high_reached", "ceiling_held", "local_low_held", "local_high_reached",
               "local_ceiling_held"}


def bars(count=10, low=D(1000), high=D(1100)):
    return [DailyBar(AS_OF + timedelta(days=i + 1), low, high, low, low, D(1)) for i in range(count)]


def test_score_window_boundaries_are_inclusive():
    # min low == low * 0.99 holds; max high == high reaches; max high == ceiling holds.
    result = card_log.score_window(ROW, bars(low=D(990), high=D(1100)))
    assert set(result) == RESULT_KEYS
    assert result["low_held"] is True and result["high_reached"] is True
    assert result["ceiling_held"] is True


def test_score_window_misses():
    result = card_log.score_window(ROW, bars(low=D("989.9"), high=D(1099)))
    assert result["low_held"] is False and result["high_reached"] is False
    assert result["local_low_held"] is True and result["local_high_reached"] is True
    result = card_log.score_window(ROW, bars(high=D(1201)))
    assert result["ceiling_held"] is False and result["local_ceiling_held"] is False
    assert card_log.score_window(ROW, bars(high=D(1150)))["local_ceiling_held"] is True


def test_score_window_needs_ten_sessions():
    assert card_log.score_window(ROW, bars(9)) is None
    assert card_log.score_window(ROW, []) is None


def card(low="9950", high="10150", ceiling="10250", sources=("ai", "ai", "local")):
    levels = {key: {"price": D(price), "source": source} for key, price, source in
              zip(("buy_zone", "sell_zone", "peak_limit"), (low, high, ceiling), sources)}
    return {"current_price": D(10050), "levels": levels}


LOCAL = {"low": {"price": D(9900)}, "high": {"price": D(10200)}, "ceiling": {"price": D(10300)}}


def test_record_card_upserts_and_resets_scores(db):
    async def run():
        await card_log.record_card("005930", AS_OF, card(), LOCAL, D(120), "qwen3.7-plus")
        async with connection() as conn:
            await conn.execute("UPDATE price_card_log SET scored_at = now(), low_held = true, "
                               "local_high_reached = false")
        await card_log.record_card("005930", AS_OF, card(low="9960"), LOCAL, None, "local")
        async with connection(row_factory=dict_row) as conn:
            return await (await conn.execute("SELECT * FROM price_card_log")).fetchall()

    rows = asyncio.run(run())
    assert len(rows) == 1
    row = rows[0]
    assert (row["low"], row["high"], row["ceiling"]) == (D(9960), D(10150), D(10250))
    assert (row["local_low"], row["local_high"], row["local_ceiling"]) == (D(9900), D(10200), D(10300))
    assert row["current_price"] == D(10050) and row["atr14"] is None and row["model"] == "local"
    assert (row["low_source"], row["high_source"], row["ceiling_source"]) == ("ai", "ai", "local")
    assert all(row[key] is None for key in ("scored_at", "low_held", "high_reached", "ceiling_held",
                                            "local_low_held", "local_high_reached", "local_ceiling_held"))


def test_card_updates_records_a_fresh_card_once(monkeypatch):
    patch(monkeypatch, analyst())
    record = AsyncMock()
    monkeypatch.setattr(price_card, "record_card", record)
    data = card_market()
    store = {}
    data.broker.redis = SimpleNamespace(get=AsyncMock(side_effect=store.get),
                                        set=AsyncMock(side_effect=lambda k, v, ex: store.update({k: v})))

    first = asyncio.run(collect(card_updates(data, "005930")))
    record.assert_awaited_once()
    code, as_of, final, local, atr, model = record.await_args.args
    assert (code, as_of) == ("005930", date(2026, 11, 20))
    assert final == first[-1]["band"]
    assert {key: level["source"] for key, level in final["levels"].items()} == {
        "buy_zone": "ai", "sell_zone": "ai", "peak_limit": "ai"}
    assert {name: level["price"] for name, level in local.items()} == {
        name: first[0]["band"]["levels"][key]["price"]
        for name, key in (("low", "buy_zone"), ("high", "sell_zone"), ("ceiling", "peak_limit"))}
    assert isinstance(atr, D) and atr > 0
    assert model == price_card.llm.model_name("analyst")

    asyncio.run(collect(card_updates(data, "005930")))  # cache hit
    record.assert_awaited_once()


def test_local_card_is_recorded_with_local_model(monkeypatch):
    patch(monkeypatch, AsyncMock(side_effect=price_card.llm.LLMUnavailable("down", reason="x")))
    record = AsyncMock()
    monkeypatch.setattr(price_card, "record_card", record)
    asyncio.run(collect(card_updates(card_market(), "005930")))
    assert record.await_args.args[-1] == "local"


def test_failing_record_card_never_breaks_the_stream(monkeypatch):
    patch(monkeypatch, analyst())
    monkeypatch.setattr(price_card, "record_card", AsyncMock(side_effect=RuntimeError("db down")))
    snapshots = asyncio.run(collect(card_updates(card_market(), "005930")))
    assert len(snapshots) == 2


def test_summary_table_reports_ai_and_local_rates():
    import importlib.util
    from pathlib import Path

    spec = importlib.util.spec_from_file_location(
        "score_price_cards", Path(__file__).parents[1] / "scripts" / "score_price_cards.py")
    script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(script)
    ai = {"low_source": "ai", "high_source": "ai", "ceiling_source": "ai"}
    rows = [{**ai, "low_held": True, "local_low_held": False, "high_reached": False, "local_high_reached": False,
             "ceiling_held": True, "local_ceiling_held": True},
            {**ai, "low_held": True, "local_low_held": True, "high_reached": True, "local_high_reached": False,
             "ceiling_held": False, "local_ceiling_held": True}]
    table = script.summary_table(rows)
    assert "scored cards: 2" in table
    assert "100.0% (2/2)" in table and "50.0% (1/2)" in table


def load_script():
    import importlib.util
    from pathlib import Path

    spec = importlib.util.spec_from_file_location(
        "score_price_cards", Path(__file__).parents[1] / "scripts" / "score_price_cards.py")
    script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(script)
    return script


def sessions(*days):
    return [DailyBar(AS_OF + timedelta(days=day), D(1000), D(1100), D(1000), D(1000), D(1)) for day in days]


def test_window_excludes_the_in_progress_bar():
    script = load_script()
    bars = sessions(*range(1, 11))  # the 10th session is the unconfirmed "today"
    chosen = script.window_bars(AS_OF, bars, AS_OF + timedelta(days=9))
    assert len(chosen) == 9 and card_log.score_window(ROW, chosen) is None


def test_window_skips_as_of_and_caps_at_ten():
    script = load_script()
    chosen = script.window_bars(AS_OF, sessions(0, *range(1, 14)), AS_OF + timedelta(days=30))
    assert [bar.session for bar in chosen] == [AS_OF + timedelta(days=d) for d in range(1, 11)]


def test_holiday_gap_scores_once_ten_sessions_exist():
    script = load_script()
    # 9 sessions inside the first 22 days, the 10th on day 29 after a long holiday cluster.
    bars = sessions(1, 2, 3, 4, 5, 6, 7, 8, 21, 29)
    assert card_log.score_window(ROW, script.window_bars(AS_OF, bars, AS_OF + timedelta(days=22))) is None
    assert card_log.score_window(ROW, script.window_bars(AS_OF, bars, AS_OF + timedelta(days=30))) is not None


def test_summary_rates_ai_levels_only_over_ai_sourced_rows():
    script = load_script()
    ai = {"low_source": "ai", "high_source": "ai", "ceiling_source": "ai"}
    rows = [
        {**ai, "low_held": True, "local_low_held": False, "high_reached": True, "local_high_reached": False,
         "ceiling_held": True, "local_ceiling_held": True},
        # Low fell back to the local level: its AI column equals the local value and must not count.
        {**ai, "low_source": "local", "low_held": False, "local_low_held": False,
         "high_reached": False, "local_high_reached": True, "ceiling_held": True, "local_ceiling_held": True},
        {"low_source": "local", "high_source": "local", "ceiling_source": "local",
         "low_held": False, "local_low_held": False, "high_reached": False, "local_high_reached": False,
         "ceiling_held": False, "local_ceiling_held": False},
    ]
    lines = {line.split()[0]: line for line in script.summary_table(rows).splitlines()[2:]}
    # AI over AI-sourced rows only; local over the same rows, then over every scored row.
    assert lines["low_held"].split()[1:] == ["100.0%", "(1/1)", "0.0%", "(0/1)", "0.0%", "(0/3)"]
    assert lines["high_reached"].split()[1:] == ["50.0%", "(1/2)", "50.0%", "(1/2)", "33.3%", "(1/3)"]
    assert lines["ceiling_held"].split()[1:] == ["100.0%", "(2/2)", "100.0%", "(2/2)", "66.7%", "(2/3)"]


def test_gemini_fallback_analyst_records_the_gemini_model(monkeypatch):
    from unittest.mock import Mock

    import httpx
    from openai import APIStatusError

    from backend.card_validation import CardAnalysis
    from test_module6_langgraph import gemini_result

    build = analyst().side_effect

    async def gemini_call(messages, **kwargs):
        return gemini_result(await build("analyst", messages[0][1], messages[1][1], CardAnalysis,
                                         timeout=price_card.ANALYST_TIMEOUT))

    patch(monkeypatch, price_card.llm.invoke)
    for key in ("ANALYST_MODEL", "FALLBACK_MODEL", "DASHSCOPE_BASE_URL"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("DASHSCOPE_API_KEY", "dash-test")
    monkeypatch.setenv("GEMINI_API_KEY", "gemini-test")
    error = APIStatusError("rate limited", response=httpx.Response(429, request=httpx.Request("POST", "https://t")),
                           body={})
    dash, google = Mock(), Mock()
    dash.return_value.with_structured_output.return_value.ainvoke = AsyncMock(side_effect=error)
    google.return_value.with_structured_output.return_value.ainvoke = AsyncMock(side_effect=gemini_call)
    monkeypatch.setattr(price_card.llm, "ChatOpenAI", dash)
    monkeypatch.setattr(price_card.llm, "ChatGoogleGenerativeAI", google)
    record = AsyncMock()
    monkeypatch.setattr(price_card, "record_card", record)

    snapshots = asyncio.run(collect(card_updates(card_market(), "005930")))
    assert snapshots[-1]["band"]["ai_status"] == "ai"
    assert dash.call_args.kwargs["model"] == "qwen3.7-plus"
    assert record.await_args.args[-1] == "gemini-3.8-flash"
