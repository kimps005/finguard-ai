import asyncio
from unittest.mock import AsyncMock, Mock, PropertyMock

from httpx import ReadTimeout, Request, Response
from langchain_core.messages import AIMessage
from langchain_openai.chat_models.base import OpenAITimeoutError
from openai import APITimeoutError
from pydantic import BaseModel
import pytest

from backend import llm


class Analysis(BaseModel):
    analysis_text: str
    citations: list[str]


def clear_models(monkeypatch):
    for name in ("ANALYST_MODEL", "ANSWER_MODEL", "FALLBACK_MODEL", "JUDGE_MODEL", "DASHSCOPE_BASE_URL"):
        monkeypatch.delenv(name, raising=False)


def test_role_defaults(monkeypatch):
    clear_models(monkeypatch)
    assert llm.model_name("analyst") == "qwen3.7-plus"
    assert llm.model_name("answer") == "qwen3.8-flash"
    assert llm.model_name("judge") == "gemini-3.8-flash"
    assert llm.model_name("fallback") == "gemini-3.8-flash"
    monkeypatch.setenv("ANSWER_MODEL", "qwen-custom")
    assert llm.model_name("answer") == "qwen-custom"


def status_error(status):
    from openai import APIStatusError
    return APIStatusError("failure", response=Response(status, request=Request("POST", "https://test")), body={})


@pytest.mark.parametrize("failure", [
    lambda: status_error(403), lambda: status_error(429), lambda: status_error(503),
    lambda: OpenAITimeoutError(Request("POST", llm.BASE_URL)),
    lambda: APITimeoutError(Request("POST", llm.BASE_URL)),
    lambda: ReadTimeout("timed out", request=Request("POST", llm.BASE_URL)),
])
def test_dashscope_failure_falls_back_once_to_gemini(monkeypatch, failure):
    clear_models(monkeypatch)
    monkeypatch.setenv("DASHSCOPE_API_KEY", "dash")
    monkeypatch.setenv("GEMINI_API_KEY", "gem")
    expected = Analysis(analysis_text="공시 위험 진단", citations=["DOC-1"])
    dash, google = Mock(), Mock()
    primary = AsyncMock(side_effect=failure())
    dash.return_value.with_structured_output.return_value.ainvoke = primary
    fallback = AsyncMock(return_value={"parsed": expected, "raw": AIMessage(content=""), "parsing_error": None})
    google.return_value.with_structured_output.return_value.ainvoke = fallback
    monkeypatch.setattr(llm, "ChatOpenAI", dash)
    monkeypatch.setattr(llm, "ChatGoogleGenerativeAI", google)

    assert asyncio.run(llm.invoke("analyst", "진단", "공시", Analysis, timeout=20)) == expected
    primary.assert_awaited_once()
    fallback.assert_awaited_once()
    assert dash.call_args.kwargs["model"] == "qwen3.7-plus" and dash.call_args.kwargs["timeout"] == 20
    assert dash.call_args.kwargs["extra_body"] == {"thinking_budget": 1024}
    assert google.call_args.kwargs["model"] == "gemini-3.8-flash"
    assert google.call_args.kwargs["timeout"] == 20


def test_client_error_does_not_fall_back(monkeypatch):
    clear_models(monkeypatch)
    monkeypatch.setenv("DASHSCOPE_API_KEY", "dash")
    monkeypatch.setenv("GEMINI_API_KEY", "gem")
    dash, google = Mock(), Mock()
    dash.return_value.with_structured_output.return_value.ainvoke = AsyncMock(side_effect=status_error(400))
    monkeypatch.setattr(llm, "ChatOpenAI", dash)
    monkeypatch.setattr(llm, "ChatGoogleGenerativeAI", google)

    with pytest.raises(llm.LLMUnavailable) as failure:
        asyncio.run(llm.invoke("answer", "진단", "공시", Analysis))
    assert failure.value.reason == "400 공급자 오류"
    google.assert_not_called()


def test_gemini_model_name_skips_dashscope(monkeypatch):
    clear_models(monkeypatch)
    monkeypatch.setenv("ANSWER_MODEL", "gemini-3.8-flash")
    monkeypatch.setenv("DASHSCOPE_API_KEY", "dash")
    monkeypatch.setenv("GEMINI_API_KEY", "gem")
    text = "### 핵심 답변\n매물 부담이 커질 수 있습니다.\n### 판단 근거\n전환 가능 물량"
    raw = AIMessage(content=[{"type": "text", "text": text}])
    request = AsyncMock(return_value={"raw": raw, "parsed": None, "parsing_error": ValueError("expected JSON")})
    google, dash = Mock(), Mock()
    google.return_value.with_structured_output.return_value.ainvoke = request
    monkeypatch.setattr(llm, "ChatGoogleGenerativeAI", google)
    monkeypatch.setattr(llm, "ChatOpenAI", dash)

    assert asyncio.run(llm.invoke("answer", "진단", "공시", Analysis)).analysis_text == text
    dash.assert_not_called()
    assert request.await_args.kwargs["safety_settings"]
    assert google.call_args.kwargs["timeout"] == 60


def test_failure_log_redacts_keys(monkeypatch, caplog):
    clear_models(monkeypatch)
    monkeypatch.setenv("DASHSCOPE_API_KEY", "private-dash-key")
    monkeypatch.setenv("GEMINI_API_KEY", "private-gem-key")
    dash, google = Mock(), Mock()
    dash.return_value.with_structured_output.return_value.ainvoke = AsyncMock(
        side_effect=RuntimeError("private-dash-key"))
    monkeypatch.setattr(llm, "ChatOpenAI", dash)
    monkeypatch.setattr(llm, "ChatGoogleGenerativeAI", google)

    with pytest.raises(llm.LLMUnavailable):
        asyncio.run(llm.invoke("analyst", "진단", "공시", Analysis))
    assert "private-dash-key" not in caplog.text and "[REDACTED]" in caplog.text


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

    result = asyncio.run(llm.invoke_dashscope("answer", "qwen3.7-plus", "test", messages, Analysis, timeout=60))

    assert result == Analysis(analysis_text="공시 위험 진단", citations=["DOC-1"])
    payload = request.await_args.kwargs
    assert payload["response_format"] == {"type": "json_object"}
    assert payload["extra_body"] == {"thinking_budget": 2048}
    assert "tools" not in payload
    assert "analysis_text" in payload["messages"][0]["content"]
    assert payload["messages"][1] == {"role": "user", "content": "공시"}


@pytest.mark.parametrize(("metadata", "reason"), [
    ({"finish_reason": "SAFETY"}, "응답 안전 필터"),
    ({"finish_reason": "STOP"}, "빈 응답"),
])
def test_answer_reports_empty_and_blocked_responses(monkeypatch, metadata, reason):
    clear_models(monkeypatch)
    monkeypatch.setenv("ANSWER_MODEL", "gemini-3.8-flash")
    monkeypatch.setenv("GEMINI_API_KEY", "test")
    client = Mock()
    request = AsyncMock(return_value={
        "raw": AIMessage(content="", response_metadata=metadata),
        "parsed": None, "parsing_error": None,
    })
    client.return_value.with_structured_output.return_value.ainvoke = request
    monkeypatch.setattr(llm, "ChatGoogleGenerativeAI", client)

    with pytest.raises(llm.LLMUnavailable) as failure:
        asyncio.run(llm.invoke("answer", "진단", "공시", Analysis))
    assert failure.value.reason == reason
    request.assert_awaited_once()


def test_thinking_budget_is_chosen_by_role(monkeypatch):
    clear_models(monkeypatch)
    monkeypatch.setenv("DASHSCOPE_API_KEY", "dash")
    for role, budget in (("analyst", 1024), ("answer", 2048)):
        dash = Mock()
        dash.return_value.with_structured_output.return_value.ainvoke = AsyncMock(
            return_value=Analysis(analysis_text="x", citations=[]))
        monkeypatch.setattr(llm, "ChatOpenAI", dash)
        asyncio.run(llm.invoke(role, "진단", "공시", Analysis, timeout=60))
        assert dash.call_args.kwargs["extra_body"] == {"thinking_budget": budget}


def test_gemini_fallback_after_dashscope_timeout_is_capped_at_30_seconds(monkeypatch):
    clear_models(monkeypatch)
    monkeypatch.setenv("DASHSCOPE_API_KEY", "dash")
    monkeypatch.setenv("GEMINI_API_KEY", "gem")
    dash, google = Mock(), Mock()
    dash.return_value.with_structured_output.return_value.ainvoke = AsyncMock(
        side_effect=APITimeoutError(Request("POST", llm.BASE_URL)))
    expected = Analysis(analysis_text="공시 위험 진단", citations=[])
    google.return_value.with_structured_output.return_value.ainvoke = AsyncMock(
        return_value={"parsed": expected, "raw": AIMessage(content=""), "parsing_error": None})
    monkeypatch.setattr(llm, "ChatOpenAI", dash)
    monkeypatch.setattr(llm, "ChatGoogleGenerativeAI", google)

    assert llm.FALLBACK_TIMEOUT == 30
    assert asyncio.run(llm.invoke("analyst", "진단", "공시", Analysis, timeout=60)) == expected
    assert dash.call_args.kwargs["timeout"] == 60
    assert google.call_args.kwargs["timeout"] == 30


def test_role_configured_to_gemini_keeps_the_callers_timeout(monkeypatch):
    clear_models(monkeypatch)
    monkeypatch.setenv("ANALYST_MODEL", "gemini-3.8-flash")
    monkeypatch.setenv("DASHSCOPE_API_KEY", "dash")
    monkeypatch.setenv("GEMINI_API_KEY", "gem")
    google = Mock()
    google.return_value.with_structured_output.return_value.ainvoke = AsyncMock(
        return_value={"parsed": Analysis(analysis_text="x", citations=[]), "raw": AIMessage(content=""),
                      "parsing_error": None})
    monkeypatch.setattr(llm, "ChatGoogleGenerativeAI", google)

    asyncio.run(llm.invoke("analyst", "진단", "공시", Analysis, timeout=60))
    assert google.call_args.kwargs["timeout"] == 60


def test_dashscope_client_is_reused_within_an_event_loop(monkeypatch):
    clear_models(monkeypatch)
    monkeypatch.setenv("DASHSCOPE_API_KEY", "dash")
    dash = Mock()
    dash.return_value.with_structured_output.return_value.ainvoke = AsyncMock(
        return_value=Analysis(analysis_text="x", citations=[]))
    monkeypatch.setattr(llm, "ChatOpenAI", dash)

    async def twice(role, timeout):
        for _ in range(2):
            await llm.invoke(role, "진단", "공시", Analysis, timeout=timeout)

    asyncio.run(twice("answer", 30))
    assert dash.call_count == 1  # one HTTP pool, so the second call skips the TCP+TLS handshake
    asyncio.run(twice("answer", 30))
    assert dash.call_count == 2  # a new loop cannot use connections opened by a closed one

    async def mixed():
        await llm.invoke("answer", "진단", "공시", Analysis, timeout=30)
        await llm.invoke("analyst", "진단", "공시", Analysis, timeout=60)
        await llm.invoke("answer", "진단", "공시", Analysis, timeout=60)

    asyncio.run(mixed())
    assert dash.call_count == 5  # budget, model and timeout each select their own client
