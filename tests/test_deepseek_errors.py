"""超窗错误识别：厂商通常标成 400，靠 code 或文案区分。"""

from __future__ import annotations

from coding_agent.llm.deepseek import is_context_overflow


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
