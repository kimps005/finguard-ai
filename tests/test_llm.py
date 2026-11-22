import asyncio
from unittest.mock import AsyncMock, Mock, PropertyMock, call

from google.genai.errors import ServerError
from httpx import ReadTimeout, Request
from langchain_core.messages import AIMessage
from langchain_openai.chat_models.base import OpenAITimeoutError
from openai import APITimeoutError
from pydantic import BaseModel
import pytest

from backend import llm


class Analysis(BaseModel):
    analysis_text: str
    citations: list[str]


@pytest.mark.parametrize("error_type", [OpenAITimeoutError, APITimeoutError, ReadTimeout])
def test_worker_retries_timeout_then_switches_to_router(monkeypatch, error_type):
    for name in ("WORKER_MODEL", "ROUTER_MODEL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test")
    http_request = Request("POST", llm.BASE_URL)
    timeout = ReadTimeout("timed out", request=http_request) if error_type is ReadTimeout else error_type(http_request)
    expected = Analysis(analysis_text="공시 위험 진단", citations=["DOC-1"])
    primary = Mock()
    request = AsyncMock(side_effect=[timeout, timeout])
    primary.with_structured_output.return_value.ainvoke = request
    router = Mock()
    fallback = AsyncMock(return_value=expected)
    router.with_structured_output.return_value.ainvoke = fallback
    client = Mock(side_effect=[primary, primary, router])
    monkeypatch.setattr(llm, "ChatOpenAI", client)

    assert asyncio.run(llm.invoke("worker", "진단", "공시", Analysis)) == expected
    assert [entry.kwargs["model"] for entry in client.call_args_list] == [
        "qwen3.8-flash", "qwen3.8-flash", "qwen3.7-plus",
    ]
    assert all(entry.kwargs["timeout"] == 45.0 for entry in client.call_args_list)
    assert all(entry.kwargs["max_retries"] == 0 for entry in client.call_args_list)
    messages = request.await_args.args[0]
    assert request.await_args_list == [call(messages)] * 2
    assert messages[0][1].startswith("진단\n\n") and "JSON" in messages[0][1]
    assert messages[1] == ("human", "공시")
    fallback.assert_awaited_once_with(messages)


def test_synthesis_recovers_on_third_gemini_attempt(monkeypatch, caplog):
    monkeypatch.delenv("SYNTHESIZER_MODEL", raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "private-test-key")
    text = "### 핵심 답변\n매물 부담이 커질 수 있습니다.\n### 판단 근거\n전환 가능 물량"
    raw = AIMessage(content=[{"type": "text", "text": text}])
    unavailable = ServerError(503, {"error": {"message": "high demand private-test-key", "status": "UNAVAILABLE"}})
    request = AsyncMock(side_effect=[
        unavailable, unavailable,
        {"raw": raw, "parsed": None, "parsing_error": ValueError("expected JSON")},
    ])
    client = Mock()
    client.return_value.with_structured_output.return_value.ainvoke = request
    monkeypatch.setattr(llm, "ChatGoogleGenerativeAI", client)
    sleep = AsyncMock()
    monkeypatch.setattr(llm.asyncio, "sleep", sleep)
    monkeypatch.setattr(llm.random, "uniform", Mock(return_value=0.1))
    dashscope = Mock()
    monkeypatch.setattr(llm, "ChatOpenAI", dashscope)

    assert asyncio.run(llm.invoke("synthesizer", "진단", "공시", Analysis)).analysis_text == text
    assert [entry.kwargs["model"] for entry in client.call_args_list] == ["gemini-3.8-flash"] * 3
    assert request.await_args_list == [request.await_args] * 3
    assert sleep.await_args_list == [call(1.6), call(3.1)]
    dashscope.assert_not_called()
    assert request.await_args.kwargs["safety_settings"]
    assert "503" in caplog.text and "high demand [REDACTED]" in caplog.text
    assert "private-test-key" not in caplog.text


@pytest.mark.parametrize(("status", "override", "expected_model"), [
    (503, None, "qwen3.7-plus"),
    (504, "qwen-custom", "qwen-custom"),
])
def test_synthesis_switches_to_dashscope_after_three_failed_retries(
    monkeypatch, status, override, expected_model,
):
    for name in ("SYNTHESIZER_MODEL", "SYNTHESIZER_FALLBACK_MODEL", "ROUTER_MODEL"):
        monkeypatch.delenv(name, raising=False)
    if override:
        monkeypatch.setenv("SYNTHESIZER_FALLBACK_MODEL", override)
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test")
    gemini = AsyncMock(side_effect=ServerError(status, {"error": {"message": "high demand"}}))
    monkeypatch.setattr(llm, "invoke_gemini", gemini)
    sleep = AsyncMock()
    monkeypatch.setattr(llm.asyncio, "sleep", sleep)
    monkeypatch.setattr(llm.random, "uniform", Mock(return_value=0))
    expected = Analysis(analysis_text="공시 위험 진단", citations=["DOC-1"])
    dashscope = Mock()
    request = AsyncMock(return_value=expected)
    dashscope.return_value.with_structured_output.return_value.ainvoke = request
    monkeypatch.setattr(llm, "ChatOpenAI", dashscope)

    assert asyncio.run(llm.invoke("synthesizer", "진단", "공시", Analysis)) == expected
    messages = [("system", "진단"), ("human", "공시")]
    assert gemini.await_args_list == [call("synthesizer", "gemini-3.8-flash", messages, Analysis)] * 4
    assert sleep.await_args_list == [call(1.5), call(3.0), call(6.0)]
    dashscope.assert_called_once()
    assert dashscope.call_args.kwargs["model"] == expected_model
    request.assert_awaited_once()
    assert request.await_args.args[0][1:] == messages[1:]


def test_dashscope_parses_json_content_without_a_tool_call(monkeypatch):
    from openai.resources.chat.completions import AsyncCompletions
    from openai.types.chat import ChatCompletion

    completion = ChatCompletion.model_validate({
        "id": "test", "created": 0, "model": "qwen3.7-plus", "object": "chat.completion",
        "choices": [{
            "index": 0, "finish_reason": "stop",
            "message": {"role": "assistant", "content": '{"analysis_text":"공시 위험 진단","citations":["DOC-1"]}'},
        }],
    })
    request = AsyncMock(return_value=Mock(parse=Mock(return_value=completion)))
    monkeypatch.setattr(AsyncCompletions, "with_raw_response", PropertyMock(return_value=Mock(parse=request)))
    messages = [("system", "진단"), ("human", "공시")]

    result = asyncio.run(llm.invoke_dashscope("qwen3.7-plus", "test", messages, Analysis, timeout=60))

    assert result == Analysis(analysis_text="공시 위험 진단", citations=["DOC-1"])
    payload = request.await_args.kwargs
    assert payload["response_format"] == {"type": "json_object"}
    assert payload["extra_body"] == {"thinking_budget": llm.QWEN_THINKING_BUDGET}
    assert "tools" not in payload
    assert "analysis_text" in payload["messages"][0]["content"]
    assert payload["messages"][1] == {"role": "user", "content": "공시"}


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
