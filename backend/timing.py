"""Stage timings for the price-card and chat paths, one INFO line per stage on `finguard.timing`.

The lines carry stage names, elapsed seconds and ok/failed only: never prompts, answers or credentials.
"""

from contextlib import contextmanager
import logging
from time import perf_counter


logger = logging.getLogger("finguard.timing")
logger.setLevel(logging.INFO)
if not logger.handlers:
    # Uvicorn configures only its own loggers; without a handler these INFO lines would be dropped.
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("%(asctime)s [timing] %(message)s"))
    logger.addHandler(_handler)


def log(stage: str, seconds: float, status: str = "ok", **fields) -> None:
    extra = "".join(f" {key}={value}" for key, value in fields.items() if value is not None)
    logger.info("%s %.2fs %s%s", stage, seconds, status, extra)


@contextmanager
def timed(stage: str, **fields):
    start, status = perf_counter(), "ok"
    try:
        yield
    except BaseException:
        status = "failed"
        raise
    finally:
        log(stage, perf_counter() - start, status, **fields)


async def measured(stage: str, awaitable, **fields):
    """Await one step under `timed`, so steps inside asyncio.gather get their own lines."""
    with timed(stage, **fields):
        return await awaitable
