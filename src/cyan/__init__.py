"""Cyan —— 不依赖任何 Agent 框架实现的命令行编程智能体。"""

from __future__ import annotations

__version__ = "0.1.0"


def main() -> int:
    from .__main__ import main as _main

    return _main()


__all__ = ["main", "__version__"]
