"""解析用户输入里的 ``@path`` 文件引用，读出内容打包成 ``FileBlock``。

只做「用户主动引用」这一件事：不经过权限审批流程（用户自己在命令行里点出来的
路径，等价于他亲手把内容贴进对话框），但仍然复用 ``resolve_path`` 的工作区
沙箱校验，逃出工作区或不存在的引用一律静默忽略，留在文本里当普通字符处理。
"""

from __future__ import annotations

import re
from pathlib import Path

from ..errors import PathOutsideWorkspaceError
from ..llm.types import FileBlock
from ..prompt.files import truncate_text
from ..security.paths import display, resolve_path
from ..settings.tools import ToolLimits

_FILE_REF_RE = re.compile(r"@(\S+)")
# 中英文常见的句尾/括号标点，"@a.py。" 这种写法不该把句号也当成路径的一部分。
_TRAILING_PUNCTUATION = ".,;:!?、，。；：！？)]}」』"


def extract_file_refs(
    task: str, workspace: Path, tool_limits: ToolLimits | None = None
) -> list[FileBlock]:
    """从任务文本里挑出 ``@path`` 引用，读出文件内容打包成 ``FileBlock``。

    只处理工作区内、真实存在的普通文件；同一路径重复出现只读一次。解析失败
    （不存在、逃出工作区、读取出错）的引用直接跳过，不报错、不中断输入——
    这样 "联系我 @某人" 这种非文件引用的自然语言不会被误伤。
    """
    limits = tool_limits or ToolLimits()
    seen: set[str] = set()
    refs: list[FileBlock] = []
    for match in _FILE_REF_RE.finditer(task):
        raw = match.group(1).rstrip(_TRAILING_PUNCTUATION)
        if not raw or raw in seen:
            continue
        seen.add(raw)
        block = _load_file_ref(raw, workspace, limits)
        if block is not None:
            refs.append(block)
    return refs


def _load_file_ref(raw: str, workspace: Path, limits: ToolLimits) -> FileBlock | None:
    try:
        resolved = resolve_path(workspace, raw, must_exist=True)
    except (PathOutsideWorkspaceError, FileNotFoundError, OSError):
        return None
    if not resolved.is_file():
        return None
    try:
        if resolved.stat().st_size > limits.max_file_bytes:
            return None
        content = resolved.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    limit = limits.max_file_read_chars
    if limit > 0 and len(content) > limit:
        content, _truncated = truncate_text(content, limit)
    return FileBlock(path=display(workspace, resolved), content=content)
