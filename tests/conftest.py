"""测试共用固件：临时工作区、假 LLM、事件流驱动。"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from cyan.core.prompts import COMPACT_SYSTEM_PROMPT
from cyan.memory.extract import EXTRACT_SYSTEM_PROMPT
from cyan.core.runtime import Runtime
from cyan.core.types import ApprovalRequired, TaskFinished
from cyan.llm.base import LLMClient
from cyan.llm.types import AssistantMessage, LLMResponse, ToolCallBlock, Usage
from cyan.security.permissions import PermissionManager
from cyan.security.types import ApprovalDecision, PermissionMode
from cyan.session import Session, TodoAccess, WorkspaceAccess
from cyan.settings import AgentSettings, LLMSettings
from cyan.tools.registry import ToolRegistry, build_default_registry
from cyan.tools.types import ToolContext


@dataclass
class Env:
    settings: AgentSettings
    permissions: PermissionManager
    registry: ToolRegistry
    ctx: ToolContext
    session: Session


class FakeLLM(LLMClient):
    """按预设脚本依次返回响应。压缩用的 chat（专用 system prompt）不消耗 script。"""

    def __init__(self, script, task_errors=None, extract_script=None):
        self.model = "fake"
        self.script = list(script)
        self.task_errors = list(task_errors or [])
        self.extract_script = list(extract_script or [])
        self.calls = 0
        self.compact_requests: list[list[dict]] = []
        self.extract_requests: list[list[dict]] = []

    def chat(self, messages, tools=None):
        self.calls += 1
        if _is_compact_request(messages):
            self.compact_requests.append(list(messages))
            return LLMResponse(
                message=AssistantMessage.of(
                    "# 已完成事项\n压缩测试摘要\n# 关键文件、结论与约束\n无\n"
                    "# 未完成待办\n无\n# 当前任务停在哪\n继续"
                ),
                usage=Usage(8, 4, 12),
            )
        if _is_extract_request(messages):
            self.extract_requests.append(list(messages))
            if self.extract_script:
                item = self.extract_script.pop(0)
                if isinstance(item, LLMResponse):
                    return item
                if isinstance(item, AssistantMessage):
                    return LLMResponse(message=item, usage=Usage(4, 2, 6))
                return LLMResponse(
                    message=AssistantMessage.of(str(item)),
                    usage=Usage(4, 2, 6),
                )
            return LLMResponse(
                message=AssistantMessage.of('{"entries": []}'),
                usage=Usage(4, 2, 6),
            )
        if self.task_errors:
            raise self.task_errors.pop(0)
        item = self.script.pop(0) if self.script else AssistantMessage.of("done")
        if isinstance(item, LLMResponse):
            return item
        return LLMResponse(message=item, usage=Usage(10, 5, 15))


def _is_compact_request(messages) -> bool:
    if not messages:
        return False
    first = messages[0]
    return isinstance(first, dict) and first.get("role") == "system" and first.get("content") == COMPACT_SYSTEM_PROMPT


def _is_extract_request(messages) -> bool:
    if not messages:
        return False
    first = messages[0]
    return (
        isinstance(first, dict)
        and first.get("role") == "system"
        and first.get("content") == EXTRACT_SYSTEM_PROMPT
    )


def tool_call(name: str, args_json: str, cid: str = "c1") -> AssistantMessage:
    return AssistantMessage.of(tool_calls=[ToolCallBlock(id=cid, name=name, arguments=args_json)])


@pytest.fixture
def make_env(tmp_path):
    def _make(**overrides) -> Env:
        settings = AgentSettings(
            workspace=tmp_path,
            llm=LLMSettings(api_key="test"),
            **overrides,
        )
        permissions = PermissionManager(tmp_path)
        registry = build_default_registry()
        session = Session.create(workspace=tmp_path, system_prompt="")
        ctx = ToolContext(
            workspace=tmp_path,
            tool_limits=settings.tools,
            workspace_access=WorkspaceAccess(session),
            todos=TodoAccess(session),
        )
        return Env(settings, permissions, registry, ctx, session)

    return _make


@pytest.fixture
def env(make_env) -> Env:
    return make_env()


def make_runtime(env: Env, llm: LLMClient, session: Session | None = None) -> Runtime:
    session = session or Session.create(workspace=env.settings.workspace, system_prompt="")
    return Runtime.create(env.settings, llm, env.registry, env.permissions, session)


def drive(runtime: Runtime, task: str, decision=ApprovalDecision.ALLOW_ONCE, *, file_refs=None):
    """消费事件流，返回 (事件列表, 终止原因)。"""
    events, reply, reason = [], None, None
    stream = runtime.run(task, file_refs=file_refs)
    while True:
        try:
            event = stream.send(reply)
        except StopIteration:
            break
        events.append(event)
        reply = decision if isinstance(event, ApprovalRequired) else None
        if isinstance(event, TaskFinished):
            reason = event.reason
    return events, reason


def eval_perm(env: Env, tool, args, mode=PermissionMode.DEFAULT, always_allowed=None):
    return env.permissions.evaluate(
        tool,
        args,
        mode=mode,
        always_allowed=always_allowed if always_allowed is not None else set(),
    )
