import asyncio
from unittest.mock import AsyncMock, Mock

from google.genai.errors import ServerError
from httpx import Request
from langchain_core.messages import AIMessage
from langchain_openai.chat_models.base import OpenAITimeoutError
from pydantic import BaseModel
import pytest

from backend import llm


class Analysis(BaseModel):
    analysis_text: str
    citations: list[str]


def test_worker_switches_to_gemini_on_openai_timeout(monkeypatch):
    monkeypatch.delenv("WORKER_MODEL", raising=False)
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test")
    primary = Mock()
    request = AsyncMock(side_effect=OpenAITimeoutError(Request("POST", llm.BASE_URL)))
    primary.return_value.with_structured_output.return_value.ainvoke = request
    monkeypatch.setattr(llm, "ChatOpenAI", primary)
    expected = Analysis(analysis_text="공시 위험 진단", citations=["DOC-1"])
    fallback = AsyncMock(return_value=expected)
    monkeypatch.setattr(llm, "invoke_gemini", fallback)

    assert asyncio.run(llm.invoke("worker", "진단", "공시", Analysis)) == expected
    assert primary.call_args.kwargs["model"] == "qwen3.8-flash"
    assert primary.call_args.kwargs["timeout"] == 45.0
    assert primary.call_args.kwargs["max_retries"] == 0
    request.assert_awaited_once()
    fallback.assert_awaited_once_with("worker", "gemini-3.5-flash-lite", request.await_args.args[0], Analysis)


def test_synthesis_switches_model_once_on_capacity_error(monkeypatch, caplog):
    monkeypatch.delenv("SYNTHESIZER_MODEL", raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "private-test-key")
    text = "### 핵심 답변\n매물 부담이 커질 수 있습니다.\n### 판단 근거\n전환 가능 물량"
    raw = AIMessage(content=[{"type": "text", "text": text}])
    request = AsyncMock(side_effect=[
        ServerError(503, {"error": {"message": "high demand private-test-key", "status": "UNAVAILABLE"}}),
        {"raw": raw, "parsed": None, "parsing_error": ValueError("expected JSON")},
    ])
    client = Mock()
    client.return_value.with_structured_output.return_value.ainvoke = request
    monkeypatch.setattr(llm, "ChatGoogleGenerativeAI", client)

    assert asyncio.run(llm.invoke("synthesizer", "진단", "공시", Analysis)).analysis_text == text
    assert [call.kwargs["model"] for call in client.call_args_list] == [
        "gemini-3.8-flash", llm.GEMINI_CAPACITY_FALLBACK,
    ]
    assert request.await_args_list[0] == request.await_args_list[1]
    assert request.await_args.kwargs["safety_settings"]
    assert "503" in caplog.text and "high demand [REDACTED]" in caplog.text
    assert "private-test-key" not in caplog.text


@pytest.mark.parametrize(("metadata", "reason"), [
    ({"finish_reason": "SAFETY"}, "응답 안전 필터"),
    ({"finish_reason": "STOP"}, "빈 응답"),
])
def test_synthesis_reports_empty_and_blocked_responses(monkeypatch, metadata, reason):
    monkeypatch.setenv("GEMINI_API_KEY", "test")
    client = Mock()
    request = AsyncMock(return_value={
        "raw": AIMessage(content="", response_metadata=metadata),
        "parsed": None, "parsing_error": None,
    })
    client.return_value.with_structured_output.return_value.ainvoke = request
    monkeypatch.setattr(llm, "ChatGoogleGenerativeAI", client)

    with pytest.raises(llm.LLMUnavailable) as failure:
        asyncio.run(llm.invoke("synthesizer", "진단", "공시", Analysis))
    assert failure.value.reason == reason
    request.assert_awaited_once()
