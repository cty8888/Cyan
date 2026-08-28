"""diff 生成 —— write_file / edit_file 审批预览共用。"""

from __future__ import annotations

import difflib

from ..constants.tools.diff import DIFF_CHAR_LIMIT, DIFF_CONTEXT_LINES


def unified_diff(old: str, new: str, filename: str, context: int = DIFF_CONTEXT_LINES) -> str:
    """生成 unified diff; 无变化或过长时返回占位/截断文本."""
    diff = difflib.unified_diff(
        old.splitlines(keepends=True),
        new.splitlines(keepends=True),
        fromfile=f"a/{filename}",
        tofile=f"b/{filename}",
        n=context,
    )
    text = "".join(diff)
    if not text:
        return "(无变化)"
    return text if len(text) <= DIFF_CHAR_LIMIT else text[:DIFF_CHAR_LIMIT] + "\n... (diff 过长已截断)"
