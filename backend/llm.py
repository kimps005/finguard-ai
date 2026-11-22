"""Provider selection and failure isolation for the portfolio graph and evaluation."""

import asyncio
import os
import re
from typing import Literal

from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_openai import ChatOpenAI


Role = Literal["router", "worker", "synthesizer"]
DEFAULTS = {"router": "qwen3.7-plus", "worker": "qwen3.8-flash", "synthesizer": "gemini-3.8-flash"}
BASE_URL = "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"


class LLMUnavailable(Exception):
    """Safe boundary for missing credentials, provider failures and invalid responses."""


def model_name(role: Role) -> str:
    return os.getenv(f"{role.upper()}_MODEL", "").strip() or DEFAULTS[role]


def gemini(model=None):
    key = os.getenv("GEMINI_API_KEY", "").strip()
    if not key:
        raise LLMUnavailable("Gemini API key is missing")
    return ChatGoogleGenerativeAI(google_api_key=key, model=model or model_name("synthesizer"),
                                  vertexai=False, temperature=0, timeout=15, max_retries=1)


def quota_error(exc):
    status = getattr(exc, "status_code", None) or getattr(getattr(exc, "response", None), "status_code", None)
    if status in (403, 429):
        return True
    code = getattr(exc, "code", "") or getattr(exc, "body", "")
    return bool(re.search(r"quota|free.?tier|free.?allocation|allocation.{0,15}exhaust",
                          f"{code} {exc}", re.IGNORECASE))


async def invoke(role: Role, instruction: str, context: str, schema):
    """Use the same structured schema and sanitized messages on both providers."""
    messages = [("system", instruction), ("human", context)]
    key = os.getenv("DASHSCOPE_API_KEY", "").strip()
    try:
        if role != "synthesizer" and key:
            client = await asyncio.to_thread(
                ChatOpenAI, api_key=key, base_url=os.getenv("DASHSCOPE_BASE_URL", "").strip() or BASE_URL,
                model=model_name(role), temperature=0, timeout=15, max_retries=0)
            try:
                # DashScope supports OpenAI-compatible function calling.
                answer = await client.with_structured_output(schema, method="function_calling").ainvoke(messages)
                return schema.model_validate(answer)
            except Exception as exc:
                if not quota_error(exc):
                    raise
        client = await asyncio.to_thread(gemini)
        answer = await client.with_structured_output(schema).ainvoke(messages)
        return schema.model_validate(answer)
    except Exception as exc:
        raise LLMUnavailable(f"{role} unavailable") from exc
