"""超窗错误识别：厂商通常标成 400，靠 code 或文案区分。"""

from __future__ import annotations

from cyan.llm.deepseek import is_context_overflow


class _Exc(Exception):
    def __init__(self, message="", code=None, body=None):
        super().__init__(message)
        self.code = code
        self.body = body


def test_overflow_by_code():
    assert is_context_overflow(_Exc("bad request", code="context_length_exceeded"))


def test_overflow_by_nested_body():
    assert is_context_overflow(
        _Exc("bad request", body={"error": {"code": "context_length_exceeded"}})
    )


def test_overflow_by_message():
    assert is_context_overflow(
        _Exc("This model's maximum context length is 65536 tokens. However, you requested 80000 tokens.")
    )


def test_ordinary_bad_request_is_not_overflow():
    assert not is_context_overflow(_Exc("invalid json in tool call arguments"))


def test_chat_sends_max_tokens():
    from types import SimpleNamespace

    from cyan.llm.deepseek import DeepSeekClient
    from cyan.settings.llm import LLMSettings

    captured: dict = {}

    def fake_create(**kwargs):
        captured.update(kwargs)
        message = SimpleNamespace(content="hi", tool_calls=None)
        choice = SimpleNamespace(message=message, finish_reason="stop")
        usage = SimpleNamespace(prompt_tokens=1, completion_tokens=1, total_tokens=2)
        return SimpleNamespace(choices=[choice], usage=usage)

    client = DeepSeekClient(LLMSettings(api_key="k", max_tokens=2048))
    client._client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=fake_create))
    )
    client.chat([{"role": "user", "content": "hi"}])
    assert captured["max_tokens"] == 2048
