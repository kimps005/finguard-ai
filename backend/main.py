import asyncio
from contextlib import asynccontextmanager
from datetime import timedelta
from decimal import DecimalException
import logging
import os
from typing import Annotated, Literal
from weakref import WeakValueDictionary

from fastapi import FastAPI, Path, Query, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, ConfigDict, StringConstraints
from redis.exceptions import RedisError

from backend.broker import KST, BrokerConfigurationError, BrokerConnector, BrokerError, Candle
from backend.connectors import ExternalConnectors
from backend.dart_etl import DartETL
from backend.portfolio_graph import account_view, build_graph
from backend.portfolio_market import MarketData, search_stocks
from backend.price_bands import StockPriceUnavailable, price_band_updates, stock_price_band_updates
from backend.stock_analysis import dump
from backend.toss_broker import TossBrokerConnector, TossSettings


logger = logging.getLogger(__name__)
candle_locks: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()
PUBLIC_FIELDS = ["intent_type", "account_summary", "holdings", "quant_scorecards", "macro_context",
                 "simulation_result", "verified_stock_results", "guardrail_warnings", "final_report"]


@asynccontextmanager
async def lifespan(app: FastAPI):
    provider = os.getenv("BROKER_PROVIDER", "toss").strip().lower()
    if provider == "toss":
        connector = TossBrokerConnector.from_env(toss_settings=TossSettings.from_env())
    elif provider == "kis":
        connector = BrokerConnector.from_env()
    else:
        raise ValueError("BROKER_PROVIDER must be toss or kis")
    async with connector as broker:
        app.state.broker = broker
        app.state.dart_etl = DartETL(broker.redis, broker.http, os.getenv("DART_API_KEY"))
        app.state.external_connectors = ExternalConnectors.from_env(broker.http, broker.redis)
        app.state.market = MarketData(broker, app.state.dart_etl, app.state.external_connectors)
        app.state.graph = build_graph(app.state.market)
        try:
            yield
        finally:
            await app.state.dart_etl.close()


app = FastAPI(title="FinGuard AI", lifespan=lifespan)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/v1/account")
async def account_snapshot(request: Request):
    market = request.app.state.market
    try:
        balance = await market.broker.get_balance()
    except BrokerConfigurationError as exc:
        logger.warning("%s", exc)
        return JSONResponse(
            {"code": exc.code, "mode": exc.mode},
            status_code=503,
            headers={"Cache-Control": "no-store"},
        )
    except BrokerError as exc:
        logger.warning("Account lookup failed (%s)", type(exc).__name__)
        return JSONResponse({"code": "account_lookup_failed"}, status_code=502,
                            headers={"Cache-Control": "no-store"})
    snapshot = account_view(balance)
    orderable_cash = None
    try:
        orderable_cash = await market.orderable_cash()
    except Exception as exc:
        logger.warning("Orderable cash lookup failed (%s)", type(exc).__name__)
    snapshot["account_summary"]["orderable_cash"] = orderable_cash
    return Response(dump(snapshot), media_type="application/json", headers={"Cache-Control": "no-store"})


@app.get("/api/v1/stocks/search")
async def stock_search(q: Annotated[str, Query(min_length=1, max_length=100)]):
    try:
        return await search_stocks(q.strip())
    except Exception as exc:
        logger.warning("Stock search failed (%s)", type(exc).__name__)
        return JSONResponse({"message": "종목 검색을 완료할 수 없습니다."}, status_code=503)


@app.get("/api/v1/stocks/{stock_code}/candles")
async def stock_candles(request: Request, stock_code: Annotated[str, Path(pattern=r"^[0-9A-Za-z]{6}$")],
                        interval: Literal["10m"] = "10m"):
    broker = request.app.state.broker
    code = stock_code.upper()
    cache_key = f"finguard:candles:{interval}:{code}"
    lock = candle_locks.setdefault(cache_key, asyncio.Lock())
    async with lock:
        try:
            content = await broker.redis.get(cache_key)
        except RedisError:
            content = None
        if content is None:
            try:
                candles = await broker.get_ten_minute_candles(code)
            except (BrokerError, KeyError, TypeError, ValueError, DecimalException) as exc:
                logger.warning("Minute candles lookup failed (%s)", type(exc).__name__)
                candles = []
            if not candles:
                end = broker.clock().astimezone(KST).date()
                try:
                    bars = await broker.get_daily_bars(code, end - timedelta(days=30), end)
                    candles = [Candle(bar.session, bar.open, bar.high, bar.low, bar.close, bar.volume)
                               for bar in bars if min(bar.open, bar.high, bar.low, bar.close) > 0]
                except (BrokerError, KeyError, TypeError, ValueError, DecimalException) as exc:
                    logger.warning("Daily candles lookup failed (%s)", type(exc).__name__)
            content = dump(candles)
            try:
                await broker.redis.set(cache_key, content, ex=15)
            except RedisError:
                pass
    headers = {"Cache-Control": "no-store"}
    if content in ("[]", b"[]"):
        return JSONResponse({"message": "가격 차트를 불러올 수 없습니다."}, status_code=502, headers=headers)
    return Response(content, media_type="application/json", headers=headers)


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    user_query: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=4000)]


class PriceBandRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["price_bands"]
    target_stock_code: Annotated[str, StringConstraints(
        strip_whitespace=True, pattern=r"^[0-9A-Za-z]{6}$")] | None = None


def sse(event, data):
    return f"event: {event}\ndata: {dump(data)}\n\n"


async def chat_events(graph, query):
    yield sse("status", {"node": "router_node", "phase": "started"})
    final = {}
    try:
        async for mode, data in graph.astream(
            {"user_query": query}, stream_mode=["tasks", "custom", "values"], output_keys=PUBLIC_FIELDS,
        ):
            if mode == "tasks":
                node = data["name"]
                started = "input" in data
                if node.startswith("__") or (started and node == "router_node"):
                    continue
                phase = "started" if started else ("failed" if data.get("error") else "completed")
                yield sse("status", {"node": node, "phase": phase, "task_id": data["id"]})
            elif mode == "custom":
                yield sse("token", data)
            elif mode == "values":
                final = data
        if "final_report" not in final:
            raise RuntimeError("Graph ended before synthesis")
        yield sse("done", {"ok": True, **{k: final[k] for k in PUBLIC_FIELDS}})
    except Exception as exc:
        # Provider errors can contain credential-bearing URLs. Keep them out of SSE and logs.
        logger.error("Portfolio stream failed (%s)", type(exc).__name__)
        yield sse("error", {"message": "계좌 분석을 완료할 수 없습니다."})
        yield sse("done", {"ok": False})


async def price_band_events(market, target_stock_code=None):
    yield sse("status", {"node": "market_data_node", "phase": "started"})
    try:
        preliminary = True
        updates = (stock_price_band_updates(market, target_stock_code.upper()) if target_stock_code
                   else price_band_updates(market))
        async for snapshot in updates:
            if preliminary:
                yield sse("price_bands", snapshot)
                preliminary = False
            else:
                yield sse("done", {"ok": True, "mode": "price_bands", **snapshot})
    except StockPriceUnavailable as exc:
        yield sse("error", {"message": str(exc)})
        yield sse("done", {"ok": False})
    except Exception as exc:
        logger.error("Short-term analysis stream failed (%s)", type(exc).__name__)
        yield sse("error", {"message": "1개월 타점 분석을 완료할 수 없습니다."})
        yield sse("done", {"ok": False})


@app.post("/api/v1/chat/stream", response_class=StreamingResponse)
async def chat_stream(body: ChatRequest | PriceBandRequest, request: Request):
    events = (price_band_events(request.app.state.market, body.target_stock_code) if isinstance(body, PriceBandRequest)
              else chat_events(request.app.state.graph, body.user_query))
    return StreamingResponse(events, media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
