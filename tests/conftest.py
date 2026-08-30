"""测试共用固件：临时工作区、假 LLM、事件流驱动。"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from coding_agent.core.prompts import COMPACT_SYSTEM_PROMPT
from coding_agent.core.runtime import Runtime
from coding_agent.core.types import ApprovalRequired, TaskFinished
from coding_agent.llm.base import LLMClient
from coding_agent.llm.types import AssistantMessage, LLMResponse, ToolCallBlock, Usage
from coding_agent.security.permissions import PermissionManager
from coding_agent.security.types import ApprovalDecision, PermissionMode
from coding_agent.session import Session, WorkspaceAccess
from coding_agent.settings import AgentSettings, LLMSettings
from coding_agent.tools.registry import ToolRegistry, build_default_registry
from coding_agent.tools.types import ToolContext


@dataclass
class Env:
    settings: AgentSettings
    permissions: PermissionManager
    registry: ToolRegistry
    ctx: ToolContext
    session: Session


class FakeLLM(LLMClient):
    """按预设脚本依次返回响应。压缩用的 chat（专用 system prompt）不消耗 script。"""

    def __init__(self, script, task_errors=None):
        self.model = "fake"
        self.script = list(script)
        self.task_errors = list(task_errors or [])
        self.calls = 0
        self.compact_requests: list[list[dict]] = []

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
        )
        return Env(settings, permissions, registry, ctx, session)

    return _make


@pytest.fixture
def env(make_env) -> Env:
    return make_env()


def make_runtime(env: Env, llm: LLMClient, session: Session | None = None) -> Runtime:
    session = session or Session.create(workspace=env.settings.workspace, system_prompt="")
    return Runtime.create(env.settings, llm, env.registry, env.permissions, session)


def drive(runtime: Runtime, task: str, decision=ApprovalDecision.ALLOW_ONCE):
    """消费事件流，返回 (事件列表, 终止原因)。"""
    events, reply, reason = [], None, None
    stream = runtime.run(task)
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
