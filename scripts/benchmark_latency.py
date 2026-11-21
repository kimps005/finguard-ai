"""Measure FinGuard's first events, first token and completed SSE reports."""

import argparse
import asyncio
from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from decimal import Decimal
import json
import os
from pathlib import Path
import re
import sys
from time import perf_counter

import httpx
import psycopg

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.broker import KST
from backend.main import sse


PATH = "/api/v1/chat/stream"
FAST_QUERY = "내 잔고와 현재가를 보여 주세요."
DEEP_QUERY = "내 계좌의 공시, 수급과 주요 위험을 종합 진단해 주세요."
TARGETS = {"ttfe": 0.5, "fast_path_ttft": 1.0, "deep_warm_duration": 12.0, "deep_cold_duration": 25.0}
LABELS = {
    "ttfe": "TTFE",
    "fast_path_ttft": "Fast Path TTFT",
    "deep_warm_duration": "Deep Analysis Warm Cache Duration",
    "deep_cold_duration": "Deep Analysis Cold Cache Duration",
}


class BenchmarkError(RuntimeError):
    pass


async def read_events(lines: AsyncIterator[str]):
    event = ""
    data = []
    async for line in lines:
        if not line:
            if event in {"status", "token", "error", "done"} and data:
                try:
                    payload = json.loads("\n".join(data))
                except ValueError:
                    raise BenchmarkError("Invalid SSE JSON") from None
                if not isinstance(payload, dict):
                    raise BenchmarkError("Expected an SSE JSON object")
                yield event, payload
            event = ""
            data = []
            continue
        field, _, value = line.partition(":")
        value = value.removeprefix(" ")
        if field == "event":
            event = value
        elif field == "data":
            data.append(value)


async def measure(client, url, query, intent, *, clock=perf_counter):
    start = clock()
    ttfe = ttft = None
    tokens = []
    async with client.stream("POST", url, headers={"Accept": "text/event-stream"},
                             json={"user_query": query}) as response:
        if not response.is_success:
            raise BenchmarkError(f"SSE endpoint returned HTTP {response.status_code}")
        if response.headers.get("Content-Type", "").split(";")[0].strip().lower() != "text/event-stream":
            raise BenchmarkError("Expected Content-Type text/event-stream")
        async for event, data in read_events(response.aiter_lines()):
            elapsed = clock() - start
            if event == "status" and ttfe is None and data.get("node") == "router_node" and data.get("phase") == "started":
                ttfe = elapsed
            elif event == "token" and isinstance(data.get("text"), str):
                if data["text"].strip() and ttft is None:
                    ttft = elapsed
                tokens.append(data["text"])
            elif event == "error":
                raise BenchmarkError("Backend sent an SSE error")
            elif event == "done":
                if data.get("ok") is not True or data.get("intent_type") != intent:
                    raise BenchmarkError(f"Expected successful {intent} stream")
                if ttfe is None or ttft is None or ttfe > ttft or data.get("final_report") != "".join(tokens):
                    raise BenchmarkError("Found missing, out-of-order or inconsistent SSE events")
                return {"ttfe": ttfe, "ttft": ttft, "duration": elapsed}, data
    raise BenchmarkError("Stream closed without a complete report")


def cached_issuers(dsn, corp_codes):
    today = datetime.now(KST).date()
    with psycopg.connect(dsn, connect_timeout=5) as conn:
        rows = conn.execute(
            "SELECT DISTINCT corp_code FROM dart_chunks WHERE corp_code = ANY(%s) AND rcept_dt BETWEEN %s AND %s",
            (sorted(corp_codes), today - timedelta(days=90), today),
        ).fetchall()
    return {corp for corp, in rows}


def check_holdings(done, corp_codes):
    stocks = [row for row in done["holdings"] if row.get("asset_type") == "STOCK" and row.get("corp_code")]
    found = {row["corp_code"] for row in stocks}
    if found != corp_codes:
        raise BenchmarkError("Configured corp codes differ from the deep stream's mapped stock holdings")
    top = sorted(stocks, key=lambda row: (-Decimal(row["weight"]), row["stock_code"]))[:3]
    verified = {row["stock_code"] for row in done["verified_stock_results"] if row["analysis_text"].strip()}
    if not {row["stock_code"] for row in top} <= verified:
        raise BenchmarkError("A Top-3 stock worker did not produce verified analysis")


async def live(client, url, dsn, corp_codes, samples):
    if cached_issuers(dsn, corp_codes):
        raise BenchmarkError("Cold run requires a test DB without recent chunks for the selected issuers")
    # Fast Path can warm the broker connection without indexing any DART filings.
    fast = [(await measure(client, url, FAST_QUERY, "fast_lookup"))[0] for _ in range(samples)]
    cold, done = await measure(client, url, DEEP_QUERY, "deep_diagnosis")
    check_holdings(done, corp_codes)
    if cached_issuers(dsn, corp_codes) != corp_codes:
        raise BenchmarkError("Cold run did not populate recent DART chunks for all selected issuers")
    warm = []
    for _ in range(samples):
        timing, done = await measure(client, url, DEEP_QUERY, "deep_diagnosis")
        check_holdings(done, corp_codes)
        warm.append(timing)
    return summarize(fast, warm, cold, "live")


class FixtureStream(httpx.AsyncByteStream):
    def __init__(self, frames, advance):
        self.frames = frames
        self.advance = advance

    async def __aiter__(self):
        for timestamp, event, payload in self.frames:
            self.advance(timestamp)
            frame = sse(event, payload).replace("\n", "\r\n").encode()
            yield frame[:13]
            yield frame[13:]


async def dry_run(samples):
    """Feed the normal HTTP/SSE measurement routine a deterministic timeline."""
    now = 0.0
    plans = iter([("fast_lookup", 0.08, 0.35, 0.36)] * samples
                 + [("deep_diagnosis", 0.12, 7.0, 18.0)]
                 + [("deep_diagnosis", 0.10, 3.0, 8.0)] * samples)

    def advance(seconds):
        nonlocal now
        now = seconds

    def handle(request):
        intent, event_time, token_time, end_time = next(plans)
        expected_query = FAST_QUERY if intent == "fast_lookup" else DEEP_QUERY
        if request.method != "POST" or request.url.path != PATH or json.loads(request.content) != {
            "user_query": expected_query,
        }:
            raise BenchmarkError("Unexpected benchmark request")
        report = "한글 리포트"
        frames = [
            (event_time, "status", {"node": "router_node", "phase": "started"}),
            (token_time, "token", {"text": report}),
            (end_time, "done", {"ok": True, "intent_type": intent, "final_report": report}),
        ]
        return httpx.Response(200, headers={"Content-Type": "text/event-stream"},
                              stream=FixtureStream(frames, advance))

    async def run(client, query, intent):
        advance(0.0)
        return (await measure(client, f"http://fixture{PATH}", query, intent, clock=lambda: now))[0]

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        fast = [await run(client, FAST_QUERY, "fast_lookup") for _ in range(samples)]
        cold = await run(client, DEEP_QUERY, "deep_diagnosis")
        warm = [await run(client, DEEP_QUERY, "deep_diagnosis") for _ in range(samples)]
    return summarize(fast, warm, cold, "dry-run")


def summarize(fast, warm, cold, mode):
    metrics = {
        "ttfe": max(timing["ttfe"] for timing in [*fast, *warm, cold]),
        "fast_path_ttft": max(timing["ttft"] for timing in fast),
        "deep_warm_duration": max(timing["duration"] for timing in warm),
        "deep_cold_duration": cold["duration"],
    }
    return {"mode": mode, "unit": "seconds", "aggregation": "max", "metrics": metrics,
            "targets": TARGETS, "passed": all(value <= TARGETS[key] for key, value in metrics.items()),
            "samples": {"fast": fast, "warm": warm, "cold": [cold]}}


def markdown(report):
    lines = [f"SSE Latency · {report['mode']} · 초(s) · 최대값", "",
             "| 지표 | 결과 | 목표 |", "| --- | ---: | ---: |"]
    for key, target in TARGETS.items():
        lines.append(f"| {LABELS[key]} | {report['metrics'][key]:.3f} | ≤ {target:.1f} |")
    return "\n".join(lines)


async def run_live(args):
    async with httpx.AsyncClient(timeout=args.timeout) as client:
        async with asyncio.timeout(args.timeout * (args.samples * 2 + 1)):
            return await live(client, args.base_url.rstrip("/") + PATH, args.cache_dsn,
                              set(args.corp_code), args.samples)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--cache-dsn", default=os.getenv("LATENCY_TEST_DSN"), help="Use LATENCY_TEST_DSN to keep credentials out of argv")
    parser.add_argument("--corp-code", action="append", default=[], help="Repeat for each mapped STOCK issuer in the account")
    parser.add_argument("--samples", type=int, default=3, help="Number of Fast Path and warm samples (one cold sample)")
    parser.add_argument("--timeout", type=float, default=120.0, help="HTTP timeout in seconds")
    parser.add_argument("--dry-run", action="store_true", help="Use an offline SSE fixture and simulated clock")
    parser.add_argument("--format", choices=("json", "markdown"), default="json")
    args = parser.parse_args()
    if args.samples < 1 or not 0 < args.timeout < float("inf"):
        parser.error("--samples and --timeout must be positive")
    if not args.dry_run and (not args.cache_dsn or not args.corp_code
                             or any(not re.fullmatch(r"[0-9]{8}", code) for code in args.corp_code)):
        parser.error("Live measurement requires LATENCY_TEST_DSN or --cache-dsn and one or more 8-digit --corp-code values")
    try:
        report = asyncio.run(dry_run(args.samples) if args.dry_run else run_live(args))
    except BenchmarkError as exc:
        parser.exit(2, f"{exc}\n")
    except (httpx.HTTPError, psycopg.Error, TimeoutError) as exc:
        parser.exit(2, f"Benchmark could not complete ({type(exc).__name__}). Check the server and test database.\n")
    print(markdown(report) if args.format == "markdown" else json.dumps(report, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
