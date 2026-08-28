"""Agent 执行层：驱动循环，组合各组件，本身不保存长期状态。"""

from __future__ import annotations

from dataclasses import dataclass

from ..config import Config
from ..context.builder import ContextBuilder
from ..llm.base import LLMClient
from ..security.permissions import PermissionManager
from ..security.policy import SecurityPolicy
from ..tools.registry import ToolRegistry
from .prompts import build_system_prompt
from .session import Session
from .tool_executor import ToolExecutor


@dataclass
class Runtime:
    """Session 是数据，Runtime 是行为。

    TODO: 将 Agent Loop 从 Agent.run() 迁入 Runtime，Agent 仅作薄封装或去掉。
    TODO: 引入 EventBus，替代 Agent 直接 yield 事件。
    """

    session: Session
    config: Config
    llm: LLMClient
    context_builder: ContextBuilder
    tool_executor: ToolExecutor
    policy: SecurityPolicy
    permissions: PermissionManager

    @classmethod
    def create(
        cls,
        config: Config,
        llm: LLMClient,
        registry: ToolRegistry,
        policy: SecurityPolicy,
        permissions: PermissionManager,
        session: Session | None = None,
    ) -> Runtime:
        system_prompt = build_system_prompt(config.workspace)
        session = session or Session.create(
            workspace=config.workspace,
            system_prompt=system_prompt,
            app_config=config,
        )
        context_builder = ContextBuilder(render_mode=session.config.tool_result_mode)
        return cls(
            session=session,
            config=config,
            llm=llm,
            context_builder=context_builder,
            tool_executor=ToolExecutor(registry),
            policy=policy,
            permissions=permissions,
        )

    def messages_for_request(self) -> list[dict]:
        return self.context_builder.build_messages(
            self.session.config.system_prompt,
            self.session.messages,
            self.session.tool_history,
        )

    def sync_context_builder(self) -> None:
        """Session 配置变更后同步 ContextBuilder。"""
        self.context_builder.render_mode = self.session.config.tool_result_mode
