"""write_file / edit_file 审批预览共用的 unified diff 生成。"""

from __future__ import annotations

import difflib


def unified_diff(old: str, new: str, filename: str, context: int = 3) -> str:
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
    limit = 8000
    return text if len(text) <= limit else text[:limit] + "\n... (diff 过长已截断)"
