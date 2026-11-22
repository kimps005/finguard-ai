"""Provider selection and failure isolation for the portfolio graph and evaluation."""

import asyncio
import logging
import os
import random
import re
import traceback
from typing import Literal

from google.genai.types import HarmBlockThreshold, HarmCategory
from httpx import TimeoutException
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_openai import ChatOpenAI
from openai import APITimeoutError
from pydantic import ValidationError


Role = Literal["router", "worker", "synthesizer"]
DEFAULTS = {"router": "qwen3.7-plus", "worker": "qwen3.8-flash", "synthesizer": "gemini-3.8-flash"}
BASE_URL = "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"
SYNTHESIZER_RETRY_DELAYS = (1.5, 3.0, 6.0)
logger = logging.getLogger(__name__)


class LLMUnavailable(Exception):
    """Safe boundary for missing credentials, provider failures and invalid responses."""

    def __init__(self, message: str, *, reason: str = "LLM 호출 오류"):
        super().__init__(message)
        self.reason = reason


def model_name(role: Role) -> str:
    return os.getenv(f"{role.upper()}_MODEL", "").strip() or DEFAULTS[role]


def gemini(model=None, *, timeout=15):
    key = os.getenv("GEMINI_API_KEY", "").strip()
    if not key:
        raise LLMUnavailable("Gemini API key is missing", reason="API키 미설정")
    return ChatGoogleGenerativeAI(google_api_key=key, model=model or model_name("synthesizer"),
                                  vertexai=False, temperature=0, timeout=timeout, max_retries=1)


def http_status(exc):
    while exc is not None:
        for status in (getattr(exc, "status_code", None),
                       getattr(getattr(exc, "response", None), "status_code", None),
                       getattr(exc, "code", None)):
            if isinstance(status, int) and 400 <= status <= 599:
                return status
        exc = exc.__cause__
    return None


def quota_error(exc):
    if http_status(exc) in (403, 429):
        return True
    code = getattr(exc, "code", "") or getattr(exc, "body", "")
    return bool(re.search(r"quota|free.?tier|free.?allocation|allocation.{0,15}exhaust",
                          f"{code} {exc}", re.IGNORECASE))


def failure_reason(exc):
    if isinstance(exc, LLMUnavailable):
        return exc.reason
    status = http_status(exc)
    if status:
        return {
            401: "401 인증 오류",
            403: "403 접근 거부",
            404: "404 모델오류",
            429: "429 한도초과",
            503: "503 공급자 서비스 지연",
            504: "504 응답 시간초과",
        }.get(status, f"{status} 공급자 오류")
    if quota_error(exc):
        return "한도초과"
    while exc is not None:
        if isinstance(exc, TimeoutError) or "timeout" in type(exc).__name__.lower():
            return "응답 시간초과"
        exc = exc.__cause__
    return "LLM 호출 오류"


def log_failure(role, model, exc):
    # Provider tracebacks can contain authentication headers or credential-bearing URLs.
    detail = "".join(traceback.format_exception(exc))
    for name in ("DASHSCOPE_API_KEY", "GEMINI_API_KEY"):
        if key := os.getenv(name, "").strip():
            detail = detail.replace(key, "[REDACTED]")
    logger.exception("%s LLM failed for %s [%s]\n%s",
                     role, model, failure_reason(exc), detail, exc_info=False)


async def invoke_gemini(role, model, messages, schema):
    client = await asyncio.to_thread(gemini, model, timeout=60 if role == "synthesizer" else 15)
    options = {}
    if role == "synthesizer":
        options["safety_settings"] = {
            HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT: HarmBlockThreshold.BLOCK_NONE,
        }
    result = await client.with_structured_output(schema, include_raw=True).ainvoke(messages, **options)
    raw = result["raw"]
    metadata = raw.response_metadata
    finish = metadata.get("finish_reason")
    block = (metadata.get("prompt_feedback") or {}).get("block_reason")
    if (block and block != "BLOCK_REASON_UNSPECIFIED") or finish in {
        "SAFETY", "BLOCKLIST", "PROHIBITED_CONTENT", "SPII",
    }:
        raise LLMUnavailable(f"Gemini response blocked (prompt={block}, finish={finish})", reason="응답 안전 필터")
    parsed = result["parsed"]
    if parsed is not None and (role != "synthesizer" or parsed.analysis_text.strip()):
        return parsed

    text = raw.text.strip()
    if role == "synthesizer" and text.startswith("### "):
        # The report and its citations still pass through stock_analysis.synthesize.
        return schema.model_validate({"analysis_text": text, "citations": []})
    if not text or parsed is not None:
        raise LLMUnavailable(f"Gemini returned no analysis text (finish={finish})", reason="빈 응답")
    try:
        return schema.model_validate_json(text)
    except ValidationError as exc:
        raise LLMUnavailable(
            f"Gemini response could not be parsed (finish={finish})", reason="응답 형식 오류",
        ) from (result["parsing_error"] or exc)


async def invoke_dashscope(model, key, messages, schema, *, timeout):
    if not key:
        raise LLMUnavailable("DashScope API key is missing", reason="API키 미설정")
    client = await asyncio.to_thread(
        ChatOpenAI, api_key=key, base_url=os.getenv("DASHSCOPE_BASE_URL", "").strip() or BASE_URL,
        model=model, temperature=0, timeout=timeout, max_retries=0,
    )
    answer = await client.with_structured_output(schema, method="function_calling").ainvoke(messages)
    return schema.model_validate(answer)


async def invoke(role: Role, instruction: str, context: str, schema):
    """Use the same structured schema and sanitized messages on both providers."""
    messages = [("system", instruction), ("human", context)]
    key = os.getenv("DASHSCOPE_API_KEY", "").strip()
    model = model_name(role)
    try:
        if role == "synthesizer":
            for attempt in range(len(SYNTHESIZER_RETRY_DELAYS) + 1):
                try:
                    return await invoke_gemini(role, model, messages, schema)
                except Exception as exc:
                    if http_status(exc) not in (503, 504):
                        raise
                    log_failure(role, model, exc)
                if attempt < len(SYNTHESIZER_RETRY_DELAYS):
                    delay = SYNTHESIZER_RETRY_DELAYS[attempt]
                    await asyncio.sleep(delay + random.uniform(0, delay * 0.25))
            model = os.getenv("SYNTHESIZER_FALLBACK_MODEL", "").strip() or model_name("router")
            return await invoke_dashscope(model, key, messages, schema, timeout=60)

        if key:
            try:
                if role == "worker":
                    for _ in range(2):
                        try:
                            return await invoke_dashscope(model, key, messages, schema, timeout=45.0)
                        except (APITimeoutError, TimeoutException) as exc:
                            log_failure(role, model, exc)
                    model = model_name("router")
                    return await invoke_dashscope(model, key, messages, schema, timeout=45.0)
                return await invoke_dashscope(model, key, messages, schema, timeout=15)
            except Exception as exc:
                if not quota_error(exc):
                    raise
                log_failure(role, model, exc)
        model = model_name("synthesizer")
        return await invoke_gemini(role, model, messages, schema)
    except Exception as exc:
        log_failure(role, model, exc)
        raise LLMUnavailable(f"{role} unavailable", reason=failure_reason(exc)) from exc
