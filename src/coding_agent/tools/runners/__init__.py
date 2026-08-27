"""语言执行器注册表。

MVP 只注册 Python。扩展方式：

    from .base import CodeRunner
    class NodeRunner(CodeRunner):
        language, display_name, extension = "javascript", "Node.js", ".js"
        def build_command(self, script): return ["node", str(script)]
    register_runner(NodeRunner())
"""

from __future__ import annotations

from ...errors import InvalidToolArgumentsError
from .base import CodeRunner
from .python_runner import PythonRunner

_RUNNERS: dict[str, CodeRunner] = {}


def register_runner(runner: CodeRunner) -> CodeRunner:
    if not runner.language:
        raise ValueError(f"{type(runner).__name__} 未定义 language")
    _RUNNERS[runner.language] = runner
    return runner


def get_runner(language: str) -> CodeRunner:
    runner = _RUNNERS.get(str(language).strip().lower())
    if runner is None:
        raise InvalidToolArgumentsError(
            f"暂不支持执行 {language} 代码，当前支持：{', '.join(supported_languages())}"
        )
    return runner


def supported_languages() -> list[str]:
    return sorted(_RUNNERS)


def runtime_summary() -> dict[str, str]:
    """语言 -> 解释器绝对路径。用于告诉模型该用哪个解释器，避免它去猜 python / python3。"""
    summary = {}
    for language in sorted(_RUNNERS):
        executable = _RUNNERS[language].executable()
        if executable:
            summary[language] = executable
    return summary


register_runner(PythonRunner())

__all__ = [
    "CodeRunner",
    "register_runner",
    "get_runner",
    "supported_languages",
    "runtime_summary",
]
