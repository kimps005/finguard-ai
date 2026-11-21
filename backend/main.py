from contextlib import asynccontextmanager
import logging
import os
from typing import Annotated

from fastapi import FastAPI, Request
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, ConfigDict, StringConstraints

from backend.broker import BrokerConnector
from backend.connectors import ExternalConnectors
from backend.dart_etl import DartETL
from backend.portfolio_graph import account_view, build_graph
from backend.portfolio_market import MarketData
from backend.stock_analysis import dump


logger = logging.getLogger(__name__)
PUBLIC_FIELDS = ["intent_type", "account_summary", "holdings", "quant_scorecards", "macro_context",
                 "simulation_result", "verified_stock_results", "guardrail_warnings", "final_report"]


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with BrokerConnector.from_env() as broker:
        app.state.broker = broker
        app.state.dart_etl = DartETL(broker.redis, broker.http, os.getenv("DART_API_KEY"))
        app.state.external_connectors = ExternalConnectors.from_env(broker.http, broker.redis)
        app.state.market = MarketData(broker, app.state.dart_etl, app.state.external_connectors)
        app.state.graph = build_graph(app.state.market)
        yield


app = FastAPI(title="FinGuard AI", lifespan=lifespan)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/v1/account")
async def account_snapshot(request: Request):
    market = request.app.state.market
    balance = await market.broker.get_balance()
    snapshot = account_view(balance)
    orderable_cash = None
    try:
        orderable_cash = await market.orderable_cash()
    except Exception as exc:
        logger.warning("Orderable cash lookup failed (%s)", type(exc).__name__)
    snapshot["account_summary"]["orderable_cash"] = orderable_cash
    return Response(dump(snapshot), media_type="application/json", headers={"Cache-Control": "no-store"})


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    user_query: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=4000)]


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


@app.post("/api/v1/chat/stream", response_class=StreamingResponse)
async def chat_stream(body: ChatRequest, request: Request):
    return StreamingResponse(chat_events(request.app.state.graph, body.user_query),
                             media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
