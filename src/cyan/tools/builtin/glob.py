"""glob —— 按文件名模式查找文件，对齐 Claude Code Glob。"""

from __future__ import annotations

import re
from pathlib import Path

from ...errors import ToolError
from ...security.paths import display, resolve_path
from ...security.rules import sensitive_path
from ..base import Tool
from ..types import RiskLevel, ToolCapability, ToolContext, ToolRunResult

GLOB_NAME = "glob"
GLOB_DESCRIPTION = (
    "按文件名 glob 模式查找文件 (不是搜内容). "
    "支持 ** 递归与一层花括号, 例如 **/*.py、src/**/*.ts、*.{json,yaml}. "
    "结果按修改时间从新到旧排序, 最多返回 100 个; 触顶时会标明已截断, 请收窄 pattern. "
    "默认不尊重 .gitignore. 路径相对项目根, 不跟 bash 的 cd 走."
)
GLOB_DEFAULT_PATH = "."
GLOB_PARAMETERS = {
    "type": "object",
    "properties": {
        "pattern": {
            "type": "string",
            "description": "文件名 glob 模式, 例如 **/*.py 或 *.{json,yaml}",
        },
        "path": {
            "type": "string",
            "description": "搜索根目录, 相对于项目根. 默认工作目录根.",
            "default": GLOB_DEFAULT_PATH,
        },
    },
    "required": ["pattern"],
}

_BRACE = re.compile(r"\{([^{}]+)\}")


class GlobTool(Tool):
    name = GLOB_NAME
    description = GLOB_DESCRIPTION
    capability = ToolCapability.READ
    risk = RiskLevel.LOW
    parameters = GLOB_PARAMETERS

    def run(
        self,
        ctx: ToolContext,
        pattern: str,
        path: str = GLOB_DEFAULT_PATH,
    ) -> ToolRunResult:
        _reject_nul("pattern", pattern)
        _reject_nul("path", path)

        root = resolve_path(ctx.workspace, path, must_exist=True)
        if not root.is_dir():
            raise ToolError(f"{display(ctx.workspace, root)} 不是目录，glob 的 path 必须是目录")
        if Path(pattern).is_absolute():
            raise ToolError("pattern 必须是相对 glob，不能是绝对路径。")

        workspace = ctx.workspace.resolve()
        hits: list[Path] = []
        for expanded in _expand_braces(pattern):
            for variant in _pattern_variants(expanded):
                hits.extend(_safe_glob(root, variant, workspace))

        unique = _unique_by_resolved(hits)
        unique.sort(key=lambda item: _mtime(item), reverse=True)

        keep_sensitive = _explicit_sensitive_root(root, workspace)
        kept: list[Path] = []
        skipped_sensitive = 0
        for candidate in unique:
            relative = display(ctx.workspace, candidate)
            if sensitive_path(relative) and not keep_sensitive:
                skipped_sensitive += 1
                continue
            kept.append(candidate)

        limit = max(1, int(ctx.tool_limits.max_glob_results))
        truncated = len(kept) > limit
        shown = kept[:limit]

        if not shown:
            lines = ["No files found"]
            if skipped_sensitive:
                lines.append(f"skipped {skipped_sensitive} sensitive files")
            return ToolRunResult.success("\n".join(lines), match_count=0, truncated=False)

        lines = [display(ctx.workspace, item) for item in shown]
        if truncated:
            lines.append(
                f"... truncated, showing {limit} of {len(kept)} files (sorted by mtime). "
                "Narrow the pattern to see the rest."
            )
        if skipped_sensitive:
            lines.append(f"skipped {skipped_sensitive} sensitive files")
        return ToolRunResult.success(
            "\n".join(lines),
            match_count=len(shown),
            truncated=truncated,
        )


def _reject_nul(name: str, value: str) -> None:
    if "\x00" in (value or ""):
        raise ToolError(f"{name} 含有空字节，请去掉后再调用。")


def _pattern_variants(pattern: str) -> list[str]:
    """``**/*.py`` 在 pathlib 里不一定匹配搜索根下的 ``foo.py``，补一层非递归模式。"""
    variants = [pattern]
    if pattern.startswith("**/"):
        rest = pattern[3:]
        if rest and rest not in variants:
            variants.append(rest)
    return variants


def _expand_braces(pattern: str) -> list[str]:
    """展开一层或多层互不嵌套的 ``{a,b}``；没有逗号的花括号原样保留。"""
    match = _BRACE.search(pattern)
    if match is None:
        return [pattern]
    inner = match.group(1)
    if "," not in inner:
        return [pattern]
    start, end = match.span()
    expanded: list[str] = []
    for part in inner.split(","):
        expanded.extend(_expand_braces(pattern[:start] + part + pattern[end:]))
    return expanded


def _safe_glob(root: Path, pattern: str, workspace: Path) -> list[Path]:
    """在 root 下展开 glob；跳过 .git/、区外路径、指向区外的 symlink。"""
    found: list[Path] = []
    try:
        matches = root.glob(pattern)
    except ValueError as exc:
        raise ToolError(f"非法 glob 模式：{exc}") from exc
    for candidate in matches:
        if not _keep_hit(candidate, workspace):
            continue
        found.append(candidate)
    return found


def _keep_hit(candidate: Path, workspace: Path) -> bool:
    if candidate.is_symlink():
        try:
            resolved = candidate.resolve()
        except OSError:
            return False
        if not _in_workspace(workspace, resolved):
            return False
        if not resolved.is_file():
            return False
        if _is_git_internal(workspace, resolved):
            return False
        return True
    try:
        if not candidate.is_file():
            return False
    except OSError:
        return False
    if not _in_workspace(workspace, candidate):
        return False
    return not _is_git_internal(workspace, candidate)


def _in_workspace(workspace: Path, path: Path) -> bool:
    try:
        resolved = path.resolve()
    except OSError:
        return False
    root = workspace.resolve()
    return resolved == root or root in resolved.parents


def _is_git_internal(workspace: Path, path: Path) -> bool:
    try:
        parts = path.resolve().relative_to(workspace.resolve()).parts
    except (ValueError, OSError):
        return False
    return ".git" in parts


def _explicit_sensitive_root(root: Path, workspace: Path) -> bool:
    """搜索根已过敏感路径审批时，结果不再二次剔除。

    只认 ``sensitive_path``（与权限层同一套规则）。``.ssh`` 目录本身不算敏感，
    否则未审批就会把 ``id_rsa`` 留在结果里。
    """
    return sensitive_path(display(workspace, root)) is not None


def _unique_by_resolved(paths: list[Path]) -> list[Path]:
    seen: set[Path] = set()
    unique: list[Path] = []
    for path in paths:
        try:
            key = path.resolve()
        except OSError:
            continue
        if key in seen:
            continue
        seen.add(key)
        unique.append(path)
    return unique


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0
