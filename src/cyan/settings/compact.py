"""对话压缩策略的启动默认值。

优先级与其它域相同：命令行 / 环境变量 > 本文件默认值。
Runtime 持有一份副本，会话中途改 ``runtime.compact_policy`` 不影响 ``AgentSettings``。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class CompactPolicy:
    """何时压、留几轮。不存放会话内容。"""

    # 须贴近当前模型窗口。deepseek-chat 常见上限是 64k；写太大则压缩永远轮不到，API 一拒就是 FATAL。
    max_context_tokens: int = 64_000
    reserve_tokens: int = 3_000  # 给总结那次 chat 留口，不存放内容
    trigger_ratio: float = 0.9  # 阈值 = (max - reserve) * ratio
    keep_recent_turns: int = 2  # 优先保留的 Assistant 轮数；超窗时会降到 1 轮乃至全部压进摘要
