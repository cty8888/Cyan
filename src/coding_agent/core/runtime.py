"""Runtime：组装 LLM / 工具 / 权限 / 上下文，并持有 AgentLoop。"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any

from ..context.builder import ContextBuilder
from ..context.types import ContextPolicy
from ..llm.base import LLMClient
from ..security.permissions import PermissionManager
from ..security.types import PermissionMode
from ..session import Session, WorkspaceAccess
from ..settings import AgentSettings, CompactPolicy
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
    compact_policy: CompactPolicy
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
        compact_policy: CompactPolicy | None = None,
    ) -> Runtime:
        """装配默认的上下文策略与工具执行器，再构造 Runtime。

        ``compact_policy`` 由 App 从 ``settings.compact`` 拷贝传入。
        省略时（测试）也拷一份，避免改 Runtime 时写回启动配置。
        """
        context_policy = ContextPolicy()
        context_builder = ContextBuilder.from_policy(context_policy)
        policy = replace(compact_policy if compact_policy is not None else settings.compact)
        return cls(
            session=session,
            settings=settings,
            context_policy=context_policy,
            compact_policy=policy,
            llm=llm,
            context_builder=context_builder,
            registry=registry,
            tool_executor=ToolExecutor(registry),
            permissions=permissions,
        )

    def run(self, task: str) -> AgentStream:
        """启动一次任务，返回事件流供 CLI 消费。"""
        return self.loop.run(task)

    def schemas_for_mode(self) -> list[dict]:
        """按当前权限模式导出要交给模型的工具 schema。

        Plan 模式只暴露只读工具和 bash（后者仍由权限层拦截写操作），
        模型甚至看不到 write_file / edit_file 的定义。
        """
        if self.session.permissions.permission_mode is PermissionMode.PLAN:
            return [
                tool.to_schema()
                for tool in self.registry
                if tool.capability is ToolCapability.READ or tool.name == "bash"
            ]
        return self.registry.schemas()

    def messages_for_request(self) -> list[dict]:
        """把会话消息与工具历史渲染成发给模型的 wire 格式。"""
        return self.context_builder.build_messages(
            self.session.messages,
            self.session.tool_history,
        )

    # ------------------------------------------------------------ 行为方法
    # 供 AgentLoop 调用；AgentLoop 不应直接访问 self.llm / self.tool_executor /
    # self.permissions / self.registry —— Runtime 才是「下一步行动」的唯一入口。

    def call_llm(self, messages: list[dict], tools: list[dict] | None = None) -> LLMResponse:
        return self.llm.chat(messages, tools=tools)

    def estimate_request_tokens(self) -> int:
        """粗估下一轮任务请求的体积：组窗后的 wire + 本轮 tools schema。"""
        from ..session.compact import estimate_payload_tokens

        return estimate_payload_tokens(self.messages_for_request()) + estimate_payload_tokens(
            self.schemas_for_mode()
        )

    def needs_compact(self) -> bool:
        """当前会话是否既有可压缩区间，又达到 token 阈值。"""
        from ..session.compact import needs_compact

        return needs_compact(
            self.session,
            self.compact_policy,
            estimated_tokens=self.estimate_request_tokens(),
        )

    def compact(self, *, max_keep: int | None = None) -> bool:
        """压缩较早对话。无可切区间或 LLM 失败时返回 False，Session 保持原样。

        ``max_keep`` 限制最多保留几轮；溢出恢复传 0，把能压的历史全部收成摘要。
        """
        from ..session.compact import try_compact

        return try_compact(self.session, self.call_llm, self.compact_policy, max_keep=max_keep)

    def has_tool(self, name: str) -> bool:
        return self.registry.has(name)

    def get_tool(self, name: str) -> Tool:
        return self.registry.get(name)

    def tool_names(self) -> list[str]:
        return [tool.name for tool in self.registry]

    def execute_tool(self, name: str, args: dict[str, Any], ctx: ToolContext) -> ToolRunResult:
        """执行已通过 ``tool.validate()`` 的调用；不再重复校验参数。"""
        return self.tool_executor.execute(name, args, ctx, validated=True)

    def check_permission(
        self,
        tool: Tool,
        args: dict[str, Any],
        *,
        mode: PermissionMode,
        always_allowed: set[str],
    ) -> PermissionOutcome:
        """判定本次调用是放行、硬拒绝，还是需要用户审批。

        顺带把当前会话的 ``WorkspaceAccess`` 传给权限层，供 ``describe()``
        生成审批预览（例如写文件前读过没有、diff 用哪份原文）。
        """
        return self.permissions.evaluate(
            tool,
            args,
            mode=mode,
            always_allowed=always_allowed,
            workspace_access=WorkspaceAccess(self.session),
        )

    def apply_permission_decision(
        self,
        decision: ApprovalDecision,
        tool: Tool,
        args: dict[str, Any],
        *,
        force: bool = False,
    ) -> bool:
        """落实用户的审批选择：拒绝返回 False；允许返回 True。

        ``ALLOW_ALWAYS`` 还会把同类操作写入本会话 ``always_allowed``，
        后续同目录写入 / 同命令执行可跳过审批。``force=True`` 时只允许这一次。
        """
        return self.permissions.apply_decision(
            decision, tool, args, self.session.always_allowed, force=force
        )
