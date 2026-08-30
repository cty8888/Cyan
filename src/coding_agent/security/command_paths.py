"""从 shell 命令里抽出能看清的路径，按文件工具同一套规则判定。

解析不到的（python -c、命令替换、``$VAR``）标成 opaque，由权限层强制确认，
不在这里假装能拦住。
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path

from ..errors import PathOutsideWorkspaceError, SecurityError
from . import rules
from .messages import ENV_DUMP_MSG, OPAQUE_EXEC_MSG, UNBOUNDED_READ_MSG
from .paths import display, resolve_path
from .shell import split_command_segments

_REDIRECT = re.compile(r"(?<!&)(?:\d*)(>>?|<<?)(?!&)\s*([^\s|&;<>]+)")
_UNRESOLVED = re.compile(r"^\$|/\$\{|\$\{")

_WRITE_ALL = frozenset(
    {
        "rm",
        "rmdir",
        "mkdir",
        "touch",
        "unlink",
        "chmod",
        "chown",
        "chgrp",
        "tee",
        "truncate",
        "gzip",
        "gunzip",
        "bzip2",
        "bunzip2",
        "xz",
        "unxz",
    }
)
_INTERPRETER_NAMES = frozenset({"python", "python3", "node", "nodejs", "ruby", "perl", "php"})
_INPLACE_INTERPRETERS = frozenset({"perl", "ruby", "php"})
_WRITE_LAST = frozenset({"cp", "mv", "ln", "install"})
# 写目标在 flag 的值里，不在位置参数末尾。-o 对 grep 是 --only-matching，必须按命令头分。
_WRITE_FLAG_NEXT = {
    "curl": frozenset({"-o", "--output"}),
    "wget": frozenset({"-O", "--output-document"}),
    "install": frozenset({"-t", "--target-directory"}),
}
_WRITE_FLAG_EQ = {
    "curl": ("--output=",),
    "wget": ("--output-document=",),
    "install": ("--target-directory=",),
}
_READ_ARGS = frozenset(
    {
        "cat",
        "head",
        "tail",
        "less",
        "more",
        "wc",
        "file",
        "stat",
        "md5sum",
        "sha1sum",
        "sha256sum",
        "nl",
        "sort",
        "uniq",
        "cut",
        "grep",
        "egrep",
        "fgrep",
        "rg",
        "diff",
        "realpath",
        "readlink",
    }
)
_VALUE_FLAGS = frozenset(
    {"-n", "-c", "-C", "-o", "-O", "-f", "-d", "-t", "-w", "-I", "--file", "--directory"}
)
_OPAQUE_PATTERNS = (
    re.compile(r"`"),
    re.compile(r"\$\("),
    re.compile(r"\beval\b"),
    re.compile(r"\b(python|python3)\b[^\n]*\s-c\b"),
    re.compile(r"\b(node|nodejs)\b[^\n]*\s-e\b"),
    re.compile(r"\b(ruby|perl)\b[^\n]*\s-e\b"),
    re.compile(r">\s*\$"),
)


@dataclass(frozen=True)
class PathTouch:
    raw: str
    kind: str  # "read" | "write"


@dataclass
class CommandPathAnalysis:
    touches: list[PathTouch] = field(default_factory=list)
    opaque: bool = False
    dumps_env: bool = False
    unbounded_read: bool = False


def analyze_command(command: str) -> CommandPathAnalysis:
    """抽出路径、是否不透明、是否会倾倒环境变量。"""
    analysis = CommandPathAnalysis(
        opaque=_is_opaque(command),
        unbounded_read=_is_unbounded_read(command),
    )
    for segment in split_command_segments(command):
        _analyze_segment(segment, analysis)
    return analysis


def outside_workspace_reason(workspace: Path, command: str, cwd: Path | None = None) -> str | None:
    """命令里能看清的路径若落在工作区外，返回拒绝理由。"""
    return _outside_with_cwd_tracking(workspace, command, cwd or workspace)


def restricted_write_reason(workspace: Path, command: str, cwd: Path | None = None) -> str | None:
    """写入目标命中 Restricted 路径（如 ``.git/``）时返回理由。"""
    for relative, kind in _resolved_touches(workspace, command, cwd or workspace):
        if kind == "write":
            reason = rules.restricted_path(relative)
            if reason:
                return reason
    return None


def forced_exec_reason(workspace: Path, command: str, cwd: Path | None = None) -> str | None:
    """敏感路径、倾倒环境、或不透明命令：必须逐次确认。"""
    analysis = analyze_command(command)
    if analysis.opaque:
        return OPAQUE_EXEC_MSG
    if analysis.unbounded_read:
        return UNBOUNDED_READ_MSG
    if analysis.dumps_env:
        return ENV_DUMP_MSG
    for relative, kind in _resolved_touches(workspace, command, cwd or workspace):
        reason = rules.sensitive_path(relative)
        if reason:
            if kind == "read":
                return f"{relative} 可能包含密钥 / 凭据，读取也需要确认。"
            return reason
    return None


def reject_unsafe_paths(workspace: Path, command: str, cwd: Path | None = None) -> None:
    """执行层二次拦截：区外路径与 Restricted 写入。"""
    origin = cwd or workspace
    outside = outside_workspace_reason(workspace, command, origin)
    if outside:
        raise PathOutsideWorkspaceError(outside)
    restricted = restricted_write_reason(workspace, command, origin)
    if restricted:
        raise SecurityError(restricted)


def written_paths(workspace: Path, command: str, cwd: Path | None = None) -> list[Path]:
    """命令里能看清的写目标，解析为工作区内的绝对路径。"""
    tracked = cwd or workspace
    found: list[Path] = []
    for segment in split_command_segments(command):
        for raw, kind in _segment_touches(segment):
            if _unresolved(raw):
                continue
            try:
                resolved = resolve_path(workspace, raw, base=tracked)
            except Exception:
                continue
            if kind == "write":
                found.append(resolved)
            if _segment_is_cd(segment):
                tracked = resolved
    return found


def _outside_with_cwd_tracking(workspace: Path, command: str, start: Path) -> str | None:
    cwd = start
    for segment in split_command_segments(command):
        for raw, _kind in _segment_touches(segment):
            if _unresolved(raw):
                continue
            try:
                resolved = resolve_path(workspace, raw, base=cwd)
            except PathOutsideWorkspaceError as exc:
                return str(exc)
            except Exception:
                continue
            if _segment_is_cd(segment):
                cwd = resolved
    return None


def _resolved_touches(workspace: Path, command: str, start: Path) -> list[tuple[str, str]]:
    cwd = start
    found: list[tuple[str, str]] = []
    for segment in split_command_segments(command):
        for raw, kind in _segment_touches(segment):
            if _unresolved(raw):
                continue
            try:
                resolved = resolve_path(workspace, raw, base=cwd)
            except PathOutsideWorkspaceError:
                continue
            except Exception:
                continue
            found.append((display(workspace, resolved), kind))
            if _segment_is_cd(segment):
                cwd = resolved
    return found


def _analyze_segment(segment: str, analysis: CommandPathAnalysis) -> None:
    for raw, kind in _segment_touches(segment):
        analysis.touches.append(PathTouch(raw=raw, kind=kind))
    tokens = _tokenize(segment)
    if tokens and tokens[0] in {"printenv", "env"} and _is_bare_env(tokens):
        analysis.dumps_env = True


def _segment_touches(segment: str) -> list[tuple[str, str]]:
    touches: list[tuple[str, str]] = []
    for match in _REDIRECT.finditer(segment):
        op, target = match.group(1), match.group(2)
        if target.startswith("&"):
            continue
        kind = "write" if ">" in op else "read"
        touches.append((target, kind))

    tokens = _tokenize(segment)
    if not tokens:
        return touches
    head, *rest = tokens
    if head == "cd":
        target = rest[-1] if rest else str(Path.home())
        touches.append((target, "read"))
        return touches

    touches.extend((path, "write") for path in _write_flag_paths(tokens))
    touches.extend((path, "write") for path in _dd_write_paths(tokens))

    paths = _path_args(tokens)
    if head in _WRITE_ALL:
        touches.extend((path, "write") for path in paths)
    elif head in _WRITE_LAST and paths:
        touches.extend((path, "read") for path in paths[:-1])
        touches.append((paths[-1], "write"))
    elif head == "sed" and _sed_is_inplace(tokens):
        touches.extend((path, "write") for path in paths)
    elif _executable_name(head) in _INPLACE_INTERPRETERS and _inplace_rewrite(tokens):
        touches.extend((path, "write") for path in paths)
    elif head in _READ_ARGS:
        touches.extend((path, "read") for path in paths)
    return touches


def _dd_write_paths(tokens: list[str]) -> list[str]:
    """抽出 ``dd of=FILE`` 的写目标。"""
    if not tokens or _executable_name(tokens[0]) != "dd":
        return []
    paths: list[str] = []
    for token in tokens[1:]:
        if token.startswith("of="):
            value = token[3:]
            if value:
                paths.append(value)
    return paths


def _inplace_rewrite(tokens: list[str]) -> bool:
    """``perl -i`` / ``ruby -i`` / ``php -i`` 会原地改文件。"""
    for token in tokens[1:]:
        if token == "--":
            break
        if token in {"-i", "--inplace"} or token.startswith("-i") or token.startswith("--inplace="):
            return True
    return False


def _executable_name(token: str) -> str:
    return Path(token).name


def _write_flag_paths(tokens: list[str]) -> list[str]:
    """抽出 ``curl -o FILE`` / ``wget -O FILE`` / ``install -t DIR`` 的写目标。"""
    if not tokens:
        return []
    next_flags = _WRITE_FLAG_NEXT.get(tokens[0])
    eq_prefixes = _WRITE_FLAG_EQ.get(tokens[0], ())
    if not next_flags and not eq_prefixes:
        return []
    paths: list[str] = []
    skip_next = False
    for index, token in enumerate(tokens[1:], start=1):
        if skip_next:
            skip_next = False
            continue
        if next_flags and token in next_flags:
            if index + 1 < len(tokens):
                paths.append(tokens[index + 1])
                skip_next = True
            continue
        for prefix in eq_prefixes:
            if token.startswith(prefix):
                value = token[len(prefix) :]
                if value:
                    paths.append(value)
                break
    return paths


def _sed_is_inplace(tokens: list[str]) -> bool:
    """``sed -i`` / ``--in-place`` / ``-i.bak`` 会改文件；普通 sed 只往 stdout 打。"""
    for token in tokens[1:]:
        if token == "--":
            break
        if token in {"-i", "--in-place"} or token.startswith("-i") or token.startswith("--in-place="):
            return True
    return False


def _path_args(tokens: list[str]) -> list[str]:
    paths: list[str] = []
    skip_next = False
    for index, token in enumerate(tokens[1:], start=1):
        if skip_next:
            skip_next = False
            continue
        if token == "--":
            paths.extend(tokens[index + 1 :])
            break
        if token in _VALUE_FLAGS:
            skip_next = True
            continue
        if token.startswith("-") or re.fullmatch(r"[0-7]{3,4}", token):
            continue
        paths.append(token)
    return paths


def _tokenize(segment: str) -> list[str]:
    try:
        return shlex.split(segment)
    except ValueError:
        return segment.split()


def _is_bare_env(tokens: list[str]) -> bool:
    if tokens[0] == "printenv":
        return True
    if tokens[0] != "env":
        return False
    rest = tokens[1:]
    i = 0
    while i < len(rest):
        arg = rest[i]
        if arg == "--":
            return not rest[i + 1 :]
        if arg in {"-u", "--unset", "-C", "--chdir"}:
            i += 2
            continue
        if arg.startswith("-") and not arg.startswith("-="):
            i += 1
            continue
        if "=" in arg:
            i += 1
            continue
        return False
    return True


_RECURSIVE_SEARCH_HEADS = frozenset({"rg", "ag", "find"})
_GREP_HEADS = frozenset({"grep", "egrep", "fgrep"})
_RECURSIVE_GREP_FLAGS = frozenset({"-r", "-R", "--recursive"})


def _is_pytest_module(tokens: list[str]) -> bool:
    return _executable_name(tokens[0]) in {"python", "python3"} and tokens[1:3] == ["-m", "pytest"]


def _is_opaque(command: str) -> bool:
    if any(pattern.search(command) for pattern in _OPAQUE_PATTERNS):
        return True
    for segment in split_command_segments(command):
        tokens = _tokenize(segment)
        if (
            tokens
            and _executable_name(tokens[0]) in _INTERPRETER_NAMES
            and not _is_pytest_module(tokens)
        ):
            return True
    return False


_QUOTED = re.compile(r"'[^']*'|\"[^\"]*\"")


def _is_unbounded_read(command: str) -> bool:
    """通配或递归搜索可能扫到 .env / 密钥，不能当「看清了路径的只读」。"""
    for segment in split_command_segments(command):
        tokens = _tokenize(segment)
        if not tokens:
            continue
        head, *rest = tokens
        if _executable_name(head) in _RECURSIVE_SEARCH_HEADS:
            return True
        if _executable_name(head) in _GREP_HEADS and _has_recursive_grep_flag(rest):
            return True
        unquoted = _QUOTED.sub(" ", segment)
        unquoted_tokens = _tokenize(unquoted)
        if any(_is_glob_token(token) for token in unquoted_tokens[1:]):
            return True
    return False


def _has_recursive_grep_flag(tokens: list[str]) -> bool:
    for token in tokens:
        if token in _RECURSIVE_GREP_FLAGS:
            return True
        if token.startswith("--"):
            continue
        if token.startswith("-") and any(flag in token for flag in ("r", "R")):
            return True
    return False


def _is_glob_token(token: str) -> bool:
    return any(char in token for char in "*?[]")


def _unresolved(raw: str) -> bool:
    return bool(_UNRESOLVED.search(raw))


def _segment_is_cd(segment: str) -> bool:
    tokens = _tokenize(segment)
    return bool(tokens) and tokens[0] == "cd"
