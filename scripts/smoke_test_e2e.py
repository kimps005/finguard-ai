"""Smoke-test FP16 model loading and the live loopback FastAPI/Next.js SSE path."""

import argparse
import asyncio
from contextlib import asynccontextmanager
from datetime import date, timedelta
from decimal import Decimal
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import uvicorn

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend import dart_rag, llm, stock_analysis
from backend.broker import Balance, FiveDayFlow, Holding, InvestorFlow
from backend.main import app
from backend.portfolio_graph import build_graph
from backend.portfolio_market import ScoreInputs
from backend.sync_stock_master import Stock
from scripts.benchmark_latency import DEEP_QUERY, FAST_QUERY, PATH, read_events


ROOT = Path(__file__).resolve().parents[1]
LOOPBACK = "127.0.0.1"
VRAM_BUDGET_BYTES = 2_500_000_000
RECEIPT = "20260115000001"
STOCK = Stock("000001", "00000001", None, None, "스모크 테스트 종목", "STOCK", "26", Decimal(10000))
DISCLOSURE = "모의 공시에 전환사채 발행이 기재됐습니다"
QUERIES = {"fast_lookup": FAST_QUERY, "deep_diagnosis": DEEP_QUERY}


def verify_gpu():
    import torch

    if not torch.cuda.is_available():
        raise RuntimeError("CUDA GPU is unavailable")
    device = torch.cuda.current_device()
    torch.cuda.reset_peak_memory_stats(device)
    encoder, reranker = dart_rag._models()
    torch.cuda.synchronize(device)
    peak = torch.cuda.max_memory_allocated(device)
    print(f"GPU {torch.cuda.get_device_name(device)} | FP16 load peak {peak:,} bytes "
          f"({Decimal(peak) / 10**9:.3f} GB, limit 2.500 GB)", flush=True)
    for model in (encoder, reranker.model):
        if any(not p.is_cuda or p.dtype != torch.float16 for p in model.parameters() if p.is_floating_point()):
            raise RuntimeError("Expected both models to reside on CUDA in FP16")
    if peak > VRAM_BUDGET_BYTES:
        raise RuntimeError("Model load exceeded the VRAM budget")


def fixture_graph(first_status_received):
    as_of = date(2026, 1, 15)
    holding = Holding(STOCK.stock_code, STOCK.corp_name, STOCK.asset_type,
                      STOCK.corp_code, STOCK.isin_cd, Decimal(4), Decimal(120), Decimal(100),
                      Decimal(480), Decimal(400), Decimal(-80), Decimal("-16.67"), Decimal("0.2"))
    balance = Balance(Decimal(1600), Decimal(2000), Decimal("0.8"), (holding,))
    sessions = tuple(InvestorFlow(as_of - timedelta(days=offset), Decimal(0), Decimal(0))
                     for offset in (6, 3, 2, 1, 0))
    inputs = ScoreInputs(STOCK, FiveDayFlow(sessions, Decimal(0), Decimal(0)))

    async def get_balance():
        # Hold the fixture until the client receives a real streamed status frame.
        await first_status_received.wait()
        return balance

    async def collect():
        return await get_balance(), as_of, None, {STOCK.stock_code: inputs}

    market = SimpleNamespace(
        broker=SimpleNamespace(get_balance=get_balance, http=None),
        external=SimpleNamespace(get_news=AsyncMock(return_value=[])),
        collect=collect,
    )
    return build_graph(market)


async def wait_ready(client, url, *, exited):
    async with asyncio.timeout(25):
        while not exited():
            try:
                if (await client.get(url, timeout=1)).status_code == 200:
                    return
            except httpx.TransportError:
                pass
            await asyncio.sleep(0.1)
    raise RuntimeError("Server exited during startup")


@asynccontextmanager
async def run_backend(client, first_status_received):
    @asynccontextmanager
    async def fixture_lifespan(application):
        with patch.object(application.state, "graph", fixture_graph(first_status_received), create=True):
            yield

    rows = [{"rcept_no": RECEIPT, "report_nm": "모의 전환사채 발행결정", "chunk_text": DISCLOSURE}]
    answer = {"analysis_text": f"{DISCLOSURE} [DOC-1].", "citations": ["DOC-1"]}
    with (
        patch.object(app.router, "lifespan_context", fixture_lifespan),
        patch.object(dart_rag, "search", AsyncMock(return_value=rows)),
        patch.object(llm, "invoke", AsyncMock(side_effect=llm.LLMUnavailable(
            "Smoke test blocks cloud LLM calls", reason="오프라인 스모크 테스트"))),
        patch.object(stock_analysis, "analyze", AsyncMock(return_value=answer)),
        socket.socket() as listener,
    ):
        listener.bind((LOOPBACK, 0))
        url = f"http://{LOOPBACK}:{listener.getsockname()[1]}"
        server = uvicorn.Server(uvicorn.Config(app, workers=1, log_level="warning", timeout_graceful_shutdown=3))
        task = asyncio.create_task(server.serve(sockets=[listener]))
        try:
            await wait_ready(client, url + "/health", exited=task.done)
            yield url
        finally:
            first_status_received.set()
            server.should_exit = True
            await task


@asynccontextmanager
async def run_frontend(client, backend_url, pin):
    server = ROOT / "frontend/.next/standalone/server.js"
    if not server.is_file():
        raise RuntimeError("Run npm run build in frontend before testing the Next.js proxy")
    with socket.socket() as listener:
        listener.bind((LOOPBACK, 0))
        port = listener.getsockname()[1]
    env = dict(os.environ, HOSTNAME=LOOPBACK, PORT=str(port), BACKEND_INTERNAL_URL=backend_url,
               APP_ACCESS_PIN=pin, NEXT_TELEMETRY_DISABLED="1")
    url = f"http://{LOOPBACK}:{port}"
    with subprocess.Popen(["node", str(server)], cwd=server.parent, env=env) as process:
        try:
            await wait_ready(client, url, exited=lambda: process.poll() is not None)
            yield url
        finally:
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=10)


async def verify_stream(client, url, intent, first_status_received, headers=None):
    tokens = []
    done = None
    first_status_received.clear()
    async with asyncio.timeout(25), client.stream(
        "POST", url, headers=headers, json={"user_query": QUERIES[intent]},
    ) as response:
        response.raise_for_status()
        if response.headers.get("Content-Type", "").split(";")[0].strip() != "text/event-stream":
            raise RuntimeError("Expected an SSE response")
        async for event, data in read_events(response.aiter_lines()):
            if not first_status_received.is_set():
                if event != "status" or data != {"node": "router_node", "phase": "started"}:
                    raise RuntimeError("Expected router status as the first SSE frame")
                first_status_received.set()
                continue
            if done is not None:
                raise RuntimeError(f"{intent} stream sent frames after done")
            if event == "error" or data.get("phase") == "failed":
                raise RuntimeError(f"{intent} stream failed at {data.get('node', event)}")
            if event == "token":
                if not isinstance(data.get("text"), str):
                    raise RuntimeError("Expected a text token")
                tokens.append(data["text"])
            if event == "done":
                done = data
    if (not done or done.get("ok") is not True or done.get("intent_type") != intent
            or not "".join(tokens).strip() or done.get("final_report") != "".join(tokens)):
        raise RuntimeError(f"{intent} did not complete with a consistent streamed report")
    if done["account_summary"]["cash_deposit"] != "1600":
        raise RuntimeError("The report does not contain the mock account snapshot")
    verified = done["verified_stock_results"]
    if intent == "deep_diagnosis" and (len(verified) != 1 or verified[0]["citations"] != [RECEIPT]
                                       or DISCLOSURE not in done["final_report"]):
        raise RuntimeError("Deep diagnosis did not carry the verified mock disclosure")


async def verify_http(*, backend_only):
    first_status_received = asyncio.Event()
    async with httpx.AsyncClient(timeout=15, trust_env=False) as client:
        async with run_backend(client, first_status_received) as backend_url:
            for intent in QUERIES:
                await verify_stream(client, backend_url + PATH, intent, first_status_received)
                print(f"PASS FastAPI {intent} status -> token -> done", flush=True)
            if backend_only:
                return
            pin = secrets.token_hex(16)
            async with run_frontend(client, backend_url, pin) as frontend_url:
                url = frontend_url + "/api/chat"
                for headers in ({}, {"X-App-Pin": pin + "invalid"}):
                    response = await client.post(url, headers=headers, json={"user_query": FAST_QUERY})
                    if response.status_code != 401:
                        raise RuntimeError(f"Missing or invalid PIN returned HTTP {response.status_code}, expected 401")
                print("PASS Next.js missing and invalid PIN -> 401", flush=True)
                for intent in QUERIES:
                    await verify_stream(client, url, intent, first_status_received, {"X-App-Pin": pin})
                    print(f"PASS Next.js valid PIN + {intent} status -> token -> done", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--gpu-only", action="store_true", help="Run just the FP16 VRAM check")
    mode.add_argument("--http-only", action="store_true", help="Run both HTTP checks without loading models")
    mode.add_argument("--backend-only", action="store_true", help="Run just the FastAPI HTTP check")
    args = parser.parse_args()
    failures = []
    if not (args.http_only or args.backend_only):
        try:
            verify_gpu()
            print("PASS GPU model load", flush=True)
        except Exception as exc:
            failures.append(("GPU", exc))
    if not args.gpu_only:
        try:
            loop_factory = asyncio.SelectorEventLoop if sys.platform == "win32" else None
            with asyncio.Runner(loop_factory=loop_factory) as runner:
                runner.run(verify_http(backend_only=args.backend_only))
        except Exception as exc:
            failures.append(("HTTP", exc))
    for name, exc in failures:
        print(f"FAIL {name}: {type(exc).__name__}: {exc}", file=sys.stderr)
    return int(bool(failures))


if __name__ == "__main__":
    raise SystemExit(main())
