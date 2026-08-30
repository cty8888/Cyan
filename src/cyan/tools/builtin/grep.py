"""grep —— 基于 ripgrep 搜索文件内容，对齐 Claude Code Grep。"""

from __future__ import annotations

import re
import shutil
from pathlib import Path

from ...errors import ToolError
from ...security.paths import display, resolve_path
from ...security.rules import sensitive_path
from ..base import Tool
from ..process import run_process
from ..types import RiskLevel, ToolCapability, ToolContext, ToolRunResult

GREP_NAME = "grep"
GREP_DESCRIPTION = (
    "在文件内容中搜索正则 (ripgrep 语法, 不是 POSIX grep). "
    "默认 output_mode=files_with_matches 只返回路径; content 返回带行号的匹配行; "
    "count 返回每文件计数和全量合计. 遵守 .gitignore; 要搜被 ignore 的文件请直接传 path. "
    "路径相对项目根, 不跟 bash 的 cd 走. 不要用 bash 的 grep/rg 代替本工具."
)
GREP_DEFAULT_PATH = "."
GREP_DEFAULT_OUTPUT_MODE = "files_with_matches"
GREP_TIMEOUT_SECONDS = 30
GREP_OUTPUT_MODES = ("files_with_matches", "content", "count")
GREP_PARAMETERS = {
    "type": "object",
    "properties": {
        "pattern": {
            "type": "string",
            "description": "ripgrep 正则. 元字符需转义, 例如 interface\\{\\}",
        },
        "path": {
            "type": "string",
            "description": "文件或目录, 相对于项目根. 默认工作目录根.",
            "default": GREP_DEFAULT_PATH,
        },
        "glob": {
            "type": "string",
            "description": "按文件名收窄, 例如 **/*.tsx",
        },
        "type": {
            "type": "string",
            "description": "按语言收窄, 对应 rg --type, 例如 py、rust",
        },
        "output_mode": {
            "type": "string",
            "enum": list(GREP_OUTPUT_MODES),
            "description": "files_with_matches (默认) / content / count",
            "default": GREP_DEFAULT_OUTPUT_MODE,
        },
        "multiline": {
            "type": "boolean",
            "description": "跨行匹配, 对应 rg --multiline",
            "default": False,
        },
        "case_insensitive": {
            "type": "boolean",
            "description": "忽略大小写, 对应 rg -i",
            "default": False,
        },
        "context": {
            "type": "integer",
            "description": "匹配行上下各 N 行, 对应 rg -C",
        },
        "context_before": {
            "type": "integer",
            "description": "匹配行之前 N 行, 对应 rg -B",
        },
        "context_after": {
            "type": "integer",
            "description": "匹配行之后 N 行, 对应 rg -A",
        },
        "offset": {
            "type": "integer",
            "description": "跳过前 N 条匹配 (content) 或前 N 个文件 (其余模式). 默认 0.",
            "default": 0,
        },
        "head_limit": {
            "type": "integer",
            "description": "最多返回多少条列出的结果. 不传表示不限制条数.",
        },
    },
    "required": ["pattern"],
}

_MATCH_LINE = re.compile(r":(\d+)(?::|$)")
_CONTEXT_LINE = re.compile(r"-(\d+)-")


class GrepTool(Tool):
    name = GREP_NAME
    description = GREP_DESCRIPTION
    capability = ToolCapability.READ
    risk = RiskLevel.LOW
    parameters = GREP_PARAMETERS

    def run(
        self,
        ctx: ToolContext,
        pattern: str,
        path: str = GREP_DEFAULT_PATH,
        glob: str | None = None,
        type: str | None = None,
        output_mode: str = GREP_DEFAULT_OUTPUT_MODE,
        multiline: bool = False,
        case_insensitive: bool = False,
        context: int | None = None,
        context_before: int | None = None,
        context_after: int | None = None,
        offset: int = 0,
        head_limit: int | None = None,
    ) -> ToolRunResult:
        _reject_nul("pattern", pattern)
        _reject_nul("path", path)
        if glob is not None:
            _reject_nul("glob", glob)
        if type is not None:
            _reject_nul("type", type)

        if shutil.which("rg") is None:
            raise ToolError("未找到 ripgrep (rg)，请安装后再使用 grep 工具。")

        target = resolve_path(ctx.workspace, path, must_exist=True)
        workspace = ctx.workspace.resolve()
        argv = _rg_argv(
            pattern,
            glob=glob,
            file_type=type,
            output_mode=output_mode,
            multiline=multiline,
            case_insensitive=case_insensitive,
            context=context,
            context_before=context_before,
            context_after=context_after,
            search_path=target,
            workspace=workspace,
        )
        result = run_process(
            argv,
            workspace,
            GREP_TIMEOUT_SECONDS,
            merge_stderr=False,
            max_output_chars=ctx.tool_limits.max_process_output_chars,
        )
        if result.timed_out:
            raise ToolError(f"grep 超时（超过 {int(GREP_TIMEOUT_SECONDS * 1000)}ms），已终止。")
        if result.exit_code not in {0, 1}:
            diagnostic = (result.stderr or result.stdout or "ripgrep 拒绝了这次搜索").strip()
            raise ToolError(diagnostic)

        offset = max(0, int(offset))
        limit = None if head_limit is None else max(0, int(head_limit))
        keep_sensitive = _explicit_sensitive_root(target, workspace)

        if output_mode == "count":
            content = _format_count(
                result.stdout,
                target,
                workspace,
                offset=offset,
                head_limit=limit,
                keep_sensitive=keep_sensitive,
            )
        elif output_mode == "content":
            has_context = any(value is not None for value in (context, context_before, context_after))
            content = _format_content(
                result.stdout,
                workspace,
                offset=offset,
                head_limit=limit,
                has_context=has_context,
                keep_sensitive=keep_sensitive,
                had_matches=result.exit_code == 0,
            )
        else:
            content = _format_files(
                result.stdout,
                workspace,
                offset=offset,
                head_limit=limit,
                keep_sensitive=keep_sensitive,
            )

        content = _truncate(content, ctx.tool_limits.max_tool_output_chars)
        return ToolRunResult.success(content)


def _rg_argv(
    pattern: str,
    *,
    glob: str | None,
    file_type: str | None,
    output_mode: str,
    multiline: bool,
    case_insensitive: bool,
    context: int | None,
    context_before: int | None,
    context_after: int | None,
    search_path: Path,
    workspace: Path,
) -> list[str]:
    argv = ["rg", "--no-config", "--color=never", "--hidden", "--glob", "!.git/", "--glob", "!.git/**"]
    if output_mode == "files_with_matches":
        argv.append("-l")
    elif output_mode == "count":
        argv.extend(["-c", "--no-heading"])
    else:
        argv.extend(["-n", "--no-heading"])
    if multiline:
        argv.extend(["--multiline", "--multiline-dotall"])
    if case_insensitive:
        argv.append("-i")
    if context is not None:
        argv.extend(["-C", str(max(0, int(context)))])
    if context_before is not None:
        argv.extend(["-B", str(max(0, int(context_before)))])
    if context_after is not None:
        argv.extend(["-A", str(max(0, int(context_after)))])
    if glob:
        argv.extend(["--glob", glob])
    if file_type:
        argv.extend(["--type", file_type])
    argv.extend(["-e", pattern, "--"])
    argv.append(display(workspace, search_path))
    return argv


def _format_files(
    stdout: str,
    workspace: Path,
    *,
    offset: int,
    head_limit: int | None,
    keep_sensitive: bool,
) -> str:
    paths = [_normalize_hit_path(line, workspace) for line in stdout.splitlines() if line.strip()]
    kept, skipped = _drop_sensitive_paths(paths, workspace, keep_sensitive)
    sliced, empty_offset = _slice(kept, offset, head_limit)
    if empty_offset:
        return _with_skipped("No entries at this offset", skipped)
    if not sliced:
        return _with_skipped("No files found", skipped)
    return _with_skipped("\n".join(sliced), skipped)


def _format_count(
    stdout: str,
    search_root: Path,
    workspace: Path,
    *,
    offset: int,
    head_limit: int | None,
    keep_sensitive: bool,
) -> str:
    rows: list[tuple[str, int]] = []
    for line in stdout.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        path_text, count = _parse_count_line(stripped, search_root, workspace)
        rows.append((path_text, count))
    kept_rows: list[tuple[str, int]] = []
    skipped = 0
    for path_text, count in rows:
        if _omit_hit(workspace, path_text, keep_sensitive):
            skipped += 1
            continue
        kept_rows.append((path_text, count))
    total = sum(count for _path, count in kept_rows)
    sliced, empty_offset = _slice(kept_rows, offset, head_limit)
    if empty_offset:
        return _with_skipped("No entries at this offset", skipped)
    if not kept_rows:
        return _with_skipped("No files found", skipped)
    lines = [f"{path_text}:{count}" for path_text, count in sliced]
    lines.append(f"total: {total}")
    return _with_skipped("\n".join(lines), skipped)


def _format_content(
    stdout: str,
    workspace: Path,
    *,
    offset: int,
    head_limit: int | None,
    has_context: bool,
    keep_sensitive: bool,
    had_matches: bool,
) -> str:
    entries = _content_entries(stdout, has_context)
    kept: list[str] = []
    skipped = 0
    for entry in entries:
        path_text = _content_entry_path(entry, workspace)
        if _omit_hit(workspace, path_text, keep_sensitive):
            skipped += 1
            continue
        kept.append(_rewrite_content_entry(entry, workspace))
    sliced, empty_offset = _slice(kept, offset, head_limit)
    if empty_offset and had_matches:
        return _with_skipped("No entries at this offset", skipped)
    if not sliced:
        return _with_skipped("No files found", skipped)
    return _with_skipped("\n--\n".join(sliced) if has_context else "\n".join(sliced), skipped)


def _content_entries(stdout: str, has_context: bool) -> list[str]:
    if has_context:
        return [block.strip("\n") for block in stdout.split("\n--\n") if block.strip()]
    return [line for line in stdout.splitlines() if line]


def _content_entry_path(entry: str, workspace: Path) -> str:
    first = entry.split("\n", 1)[0]
    raw_path, _rest = _split_rg_line(first, workspace)
    return raw_path


def _rewrite_content_entry(entry: str, workspace: Path) -> str:
    """把 rg 打出来的路径收成相对工作区。"""
    lines: list[str] = []
    for line in entry.splitlines():
        raw_path, rest = _split_rg_line(line, workspace)
        if not rest:
            lines.append(line)
            continue
        resolved = _resolve_hit(workspace, raw_path)
        shown = display(workspace, resolved) if resolved is not None else raw_path
        lines.append(shown + rest)
    return "\n".join(lines)


def _split_rg_line(line: str, workspace: Path) -> tuple[str, str]:
    """拆出 ``path:line:text`` / ``path-line-text``。优先认能落到工作区文件的最长前缀。

    非贪婪地匹配第一处 ``-123-`` 会把 ``issue-123-fix.py:1:todo`` 拆成 ``issue``。
    """
    seps = list(_MATCH_LINE.finditer(line)) or list(_CONTEXT_LINE.finditer(line))
    if not seps:
        return line, ""
    existing: list[tuple[str, str]] = []
    for match in seps:
        raw = line[: match.start()]
        rest = line[match.start() :]
        if _resolve_hit(workspace, raw) is not None:
            existing.append((raw, rest))
    if existing:
        return max(existing, key=lambda item: len(item[0]))
    # 匹配行用第一处 :N:（路径几乎不含 :digits:）；上下文用最后一处 -N-
    chosen = seps[0] if seps[0].re is _MATCH_LINE else seps[-1]
    return line[: chosen.start()], line[chosen.start() :]


def _parse_count_line(line: str, search_root: Path, workspace: Path) -> tuple[str, int]:
    if ":" not in line and line.isdigit():
        path_text = display(workspace, search_root)
        return path_text, int(line)
    path_text, _, count_text = line.rpartition(":")
    try:
        count = int(count_text)
    except ValueError:
        return _normalize_hit_path(line, workspace), 0
    return _normalize_hit_path(path_text, workspace), count


def _normalize_hit_path(raw: str, workspace: Path) -> str:
    resolved = _resolve_hit(workspace, raw)
    if resolved is None:
        return raw.replace("\\", "/")
    return display(workspace, resolved)


def _resolve_hit(workspace: Path, raw: str) -> Path | None:
    text = raw.strip()
    if not text:
        return None
    candidate = Path(text)
    try:
        if not candidate.is_absolute():
            candidate = workspace / candidate
        resolved = candidate.resolve()
    except OSError:
        return None
    root = workspace.resolve()
    if resolved != root and root not in resolved.parents:
        return None
    return resolved


def _drop_sensitive_paths(
    paths: list[str], workspace: Path, keep_sensitive: bool
) -> tuple[list[str], int]:
    kept: list[str] = []
    skipped = 0
    for path_text in paths:
        if _omit_hit(workspace, path_text, keep_sensitive):
            skipped += 1
            continue
        kept.append(path_text)
    return kept, skipped


def _omit_hit(workspace: Path, path_text: str, keep_sensitive: bool) -> bool:
    if keep_sensitive or not path_text:
        return False
    resolved = _resolve_hit(workspace, path_text)
    if resolved is not None:
        return sensitive_path(display(workspace, resolved)) is not None
    return sensitive_path(path_text) is not None


def _explicit_sensitive_root(root: Path, workspace: Path) -> bool:
    """只认权限层同一套 ``sensitive_path``，避免 ``path=.ssh`` 未审批却保留私钥。"""
    return sensitive_path(display(workspace, root)) is not None


def _slice(items: list, offset: int, head_limit: int | None) -> tuple[list, bool]:
    """返回 (切片, 是否属于「有结果但 offset 越界」)。"""
    if offset and offset >= len(items) and items:
        return [], True
    sliced = items[offset:]
    if head_limit is not None:
        sliced = sliced[:head_limit]
    return sliced, False


def _with_skipped(text: str, skipped: int) -> str:
    if skipped <= 0:
        return text
    return f"{text}\nskipped {skipped} sensitive files"


def _truncate(text: str, limit: int) -> str:
    if limit <= 0 or len(text) <= limit:
        return text
    return text[:limit] + "...[truncated]"


def _reject_nul(name: str, value: str) -> None:
    if "\x00" in (value or ""):
        raise ToolError(f"{name} 含有空字节，请去掉后再调用。")
