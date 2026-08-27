"""异常体系。

划分为两大类：

- ``AgentError`` 的子类中，凡继承 ``ToolError`` 的都是**可预期失败**，会被 Registry
  捕获并转换成 ``ToolResult`` 回喂给模型，由模型自行决定如何恢复，不会中断 Agent Loop。
- 其余异常属于**不可恢复错误**，会向上冒泡终止当前任务。
"""

from __future__ import annotations


class AgentError(Exception):
    """所有自定义异常的基类。"""


class ConfigError(AgentError):
    """配置缺失或非法。"""


class UserAbort(AgentError):
    """用户主动中断（Ctrl-C）。"""


# --------------------------------------------------------------------------
# LLM 相关
# --------------------------------------------------------------------------


class LLMError(AgentError):
    """与模型服务交互失败。"""

    retryable: bool = False


class LLMAuthError(LLMError):
    """API Key 无效或权限不足。"""


class LLMRateLimitError(LLMError):
    """触发限流。"""

    retryable = True


class LLMConnectionError(LLMError):
    """网络异常或服务端 5xx。"""

    retryable = True


class LLMResponseError(LLMError):
    """响应结构不符合预期，无法解析。"""


# --------------------------------------------------------------------------
# 工具相关：以下异常都会被转换成 ToolResult 回喂模型
# --------------------------------------------------------------------------


class ToolError(AgentError):
    """工具执行过程中的可预期失败。"""


class ToolNotFoundError(ToolError):
    """模型调用了未注册的工具。"""


class InvalidToolArgumentsError(ToolError):
    """工具参数缺失、类型错误或 JSON 无法解析。"""


class SecurityError(ToolError):
    """被安全策略拦截。"""


class PathOutsideWorkspaceError(SecurityError):
    """路径逃逸出工作目录沙箱。"""


class BlockedCommandError(SecurityError):
    """命中命令黑名单，任何模式下都不允许执行。"""


class ApprovalDeniedError(ToolError):
    """用户拒绝了本次工具调用。"""
