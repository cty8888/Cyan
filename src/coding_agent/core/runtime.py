"""Runtime：组装 LLM / 工具 / 权限 / 上下文，并持有 AgentLoop。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from ..context.builder import ContextBuilder
from ..context.types import ContextPolicy
from ..llm.base import LLMClient
from ..security.permissions import PermissionManager
from ..security.types import PermissionMode
from ..session import Session, WorkspaceAccess
from ..settings import AgentSettings
from ..tools.registry import ToolRegistry
from ..tools.types import ToolCapability
from .loop import AgentLoop
from .tool_executor import ToolExecutor
from .types import AgentStream

if TYPE_CHECKING:
    from ..llm.types import LLMResponse
    from ..security.types import ApprovalDecision, PermissionOutcome
    from ..tools.base import Tool
    from ..tools.types import ToolContext, ToolRunResult


@dataclass
class Runtime:
    """Session 是数据，Runtime 组装行为组件并持有 Loop。"""

    session: Session
    settings: AgentSettings
    context_policy: ContextPolicy
    llm: LLMClient
    context_builder: ContextBuilder
    registry: ToolRegistry
    tool_executor: ToolExecutor
    permissions: PermissionManager
    loop: AgentLoop = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self.loop = AgentLoop(self)

    @classmethod
    def create(
        cls,
        settings: AgentSettings,
        llm: LLMClient,
        registry: ToolRegistry,
        permissions: PermissionManager,
        session: Session,
    ) -> Runtime:
        context_policy = ContextPolicy()
        context_builder = ContextBuilder.from_policy(context_policy)
        return cls(
            session=session,
            settings=settings,
            context_policy=context_policy,
            llm=llm,
            context_builder=context_builder,
            registry=registry,
            tool_executor=ToolExecutor(registry),
            permissions=permissions,
        )

    def run(self, task: str) -> AgentStream:
        return self.loop.run(task)

    def schemas_for_mode(self) -> list[dict]:
        if self.session.permissions.permission_mode is PermissionMode.PLAN:
            return [
                tool.to_schema()
                for tool in self.registry
                if tool.capability is ToolCapability.READ or tool.name == "bash"
            ]
        return self.registry.schemas()

    def messages_for_request(self) -> list[dict]:
        return self.context_builder.build_messages(
            self.session.messages,
            self.session.tool_history,
        )

    # ------------------------------------------------------------ 行为方法
    # 供 AgentLoop 调用；AgentLoop 不应直接访问 self.llm / self.tool_executor /
    # self.permissions / self.registry —— Runtime 才是「下一步行动」的唯一入口。

    def call_llm(self, messages: list[dict], tools: list[dict] | None = None) -> LLMResponse:
        return self.llm.chat(messages, tools=tools)

    def has_tool(self, name: str) -> bool:
        return self.registry.has(name)

    def get_tool(self, name: str) -> Tool:
        return self.registry.get(name)

    def tool_names(self) -> list[str]:
        return [tool.name for tool in self.registry]

    def execute_tool(self, name: str, args: dict[str, Any], ctx: ToolContext) -> ToolRunResult:
        return self.tool_executor.execute(name, args, ctx, validated=True)

    def check_permission(
        self,
        tool: Tool,
        args: dict[str, Any],
        *,
        mode: PermissionMode,
        always_allowed: set[str],
    ) -> PermissionOutcome:
        return self.permissions.evaluate(
            tool,
            args,
            mode=mode,
            always_allowed=always_allowed,
            workspace_access=WorkspaceAccess(self.session),
        )

    def apply_permission_decision(
        self, decision: ApprovalDecision, tool: Tool, args: dict[str, Any]
    ) -> bool:
        return self.permissions.apply_decision(decision, tool, args, self.session.always_allowed)
