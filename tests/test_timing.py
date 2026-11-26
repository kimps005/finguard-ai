import asyncio
import logging

import pytest

from backend import timing


def test_timed_logs_stage_seconds_and_fields(caplog):
    with caplog.at_level(logging.INFO, logger="finguard.timing"):
        with timing.timed("card.news", code="005930", skipped=None):
            pass
    [record] = caplog.records
    stage, seconds, status, field = record.getMessage().split()
    assert (stage, status, field) == ("card.news", "ok", "code=005930")
    assert seconds.endswith("s") and float(seconds[:-1]) >= 0


def test_timed_marks_failures_and_reraises(caplog):
    with caplog.at_level(logging.INFO, logger="finguard.timing"):
        with pytest.raises(ValueError):
            with timing.timed("llm", role="answer"):
                raise ValueError("boom")
    assert caplog.records[0].getMessage().split()[2] == "failed"
    assert "boom" not in caplog.text


def test_measured_returns_the_awaited_value(caplog):
    async def value():
        return 7

    with caplog.at_level(logging.INFO, logger="finguard.timing"):
        assert asyncio.run(timing.measured("card.chart", value())) == 7
    assert caplog.records[0].getMessage().startswith("card.chart ")
