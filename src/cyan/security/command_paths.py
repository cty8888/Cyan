"""从 shell 命令里抽出能看清的路径，按文件工具同一套规则判定。

解析不到的（python -c、命令替换、``$VAR``）标成 opaque，路径层不当成已看清；
权限上走普通执行审批，``allow`` 可以放行。
``cd "$HOME"`` / ``popd`` 这类看不清目标的改目录命令直接拒绝，不在区外执行。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from ..errors import PathOutsideWorkspaceError, SecurityError
from . import rules
from .catalog import shell_catalog
from .floor import critical_rm_reason
from .messages import ENV_DUMP_MSG, UNBOUNDED_READ_MSG, UNRESOLVED_CHDIR_MSG
from .paths import display, resolve_path
from .shell import (
    executable_name,
    peel_git_globals,
    split_command_segments,
    tokenize,
    unwrap_argv,
)

_REDIRECT = re.compile(r"(?<!&)(?:\d*)(>>?|<<?)(?!&)\s*([^\s|&;<>]+)")
_UNRESOLVED = re.compile(r"^\$|/\$\{|\$\{")

# 位置参数里「后面跟值」的通用 flag，和具体命令名单无关。
_VALUE_FLAGS = frozenset(
    {"-n", "-c", "-C", "-o", "-O", "-f", "-d", "-t", "-w", "-I", "--file", "--directory"}
)
_OPAQUE_PATTERNS = (
    re.compile(r"`"),
    re.compile(r"\$\("),
    re.compile(r"<\("),
    re.compile(r">\("),
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
    """写入目标命中 write/read deny（如 ``.git/``、``Read`` deny 连带挡写）时返回理由。"""
    for relative, kind in _resolved_touches(workspace, command, cwd or workspace):
        if kind == "write":
            reason = rules.restricted_path(relative)
            if reason:
                return reason
    return None


def denied_path_reason(workspace: Path, command: str, cwd: Path | None = None) -> str | None:
    """bash 能看清的读/写路径命中 deny 规则。read deny 也挡住写入。"""
    for relative, kind in _resolved_touches(workspace, command, cwd or workspace):
        if kind == "write":
            reason = rules.restricted_path(relative)
        else:
            reason = rules.read_denied_path(relative)
        if reason:
            return reason
    return None


def forced_exec_reason(workspace: Path, command: str, cwd: Path | None = None) -> str | None:
    """敏感路径、倾倒环境、无界读取：必须逐次确认。不透明命令走普通审批，allow 可以放行。"""
    analysis = analyze_command(command)
    if analysis.unbounded_read:
        return UNBOUNDED_READ_MSG
    if analysis.dumps_env:
        return ENV_DUMP_MSG
    for relative, kind in _resolved_touches(workspace, command, cwd or workspace):
        if kind == "write":
            reason = rules.write_confirm_path(relative)
            if reason:
                return reason
            continue
        reason = rules.sensitive_path(relative)
        if reason:
            return f"{relative} 可能包含密钥 / 凭据，读取也需要确认。"
    return None


def reject_unsafe_paths(workspace: Path, command: str, cwd: Path | None = None) -> None:
    """执行层二次拦截：区外路径与 deny 路径。"""
    origin = cwd or workspace
    outside = outside_workspace_reason(workspace, command, origin)
    if outside:
        raise PathOutsideWorkspaceError(outside)
    denied = denied_path_reason(workspace, command, origin)
    if denied:
        raise SecurityError(denied)


def written_paths(workspace: Path, command: str, cwd: Path | None = None) -> list[Path]:
    """命令里能看清的写目标，解析为工作区内的绝对路径。"""
    return [
        path
        for _relative, kind, path in _walk_resolved(workspace, command, cwd, ignore_outside=True)
        if kind == "write"
    ]


def _outside_with_cwd_tracking(workspace: Path, command: str, start: Path) -> str | None:
    try:
        list(_walk_resolved(workspace, command, start, ignore_outside=False))
    except PathOutsideWorkspaceError as exc:
        return str(exc)
    return None


def _resolved_touches(workspace: Path, command: str, start: Path) -> list[tuple[str, str]]:
    return [
        (relative, kind)
        for relative, kind, _path in _walk_resolved(workspace, command, start, ignore_outside=True)
    ]


def _walk_resolved(
    workspace: Path,
    command: str,
    start: Path | None,
    *,
    ignore_outside: bool,
) -> list[tuple[str, str, Path]]:
    """按段解析路径。``env -C`` / ``git -C`` 只影响该段内层命令，不影响重定向。"""
    found, _cwd = _walk_from(workspace, command, start or workspace, ignore_outside=ignore_outside)
    return found


def _walk_from(
    workspace: Path,
    command: str,
    cwd: Path,
    *,
    ignore_outside: bool,
) -> tuple[list[tuple[str, str, Path]], Path]:
    """从 ``cwd`` 起按段走命令，返回触点与走完后的 shell cwd。

    ``(...)`` 子 shell 不改外层 cwd；``{...}`` 当前 shell 分组会改。
    """
    found: list[tuple[str, str, Path]] = []
    for segment in split_command_segments(command):
        kind, inner = _strip_compound(segment)
        if kind == "subshell":
            inner_found, _ignored = _walk_from(
                workspace, inner, cwd, ignore_outside=ignore_outside
            )
            found.extend(inner_found)
            continue
        if kind == "group":
            inner_found, cwd = _walk_from(
                workspace, inner, cwd, ignore_outside=ignore_outside
            )
            found.extend(inner_found)
            continue

        plan = _plan_segment(segment)
        resolved_here: list[tuple[str, str, Path]] = []

        def add(raw: str, kind: str, base: Path) -> Path | None:
            try:
                item = _try_resolve(workspace, raw, base, ignore_outside=ignore_outside)
            except PathOutsideWorkspaceError:
                # 关键 rm 的落点（/、家目录、工作区父目录）交给强制询问，不当成区外拒绝。
                if (
                    not ignore_outside
                    and kind == "write"
                    and critical_rm_reason(segment, workspace=workspace, cwd=cwd)
                ):
                    return None
                raise
            if item is None:
                return None
            resolved_here.append((item[0], kind, item[1]))
            return item[1]

        for raw, kind in plan.shell_touches:
            add(raw, kind, cwd)

        proc_base = cwd
        if plan.proc_chdir:
            resolved = add(plan.proc_chdir, "read", cwd)
            if resolved is not None:
                proc_base = resolved

        if plan.git_chdir:
            resolved = add(plan.git_chdir, "read", proc_base)
            if resolved is not None:
                proc_base = resolved

        for raw, kind in plan.inner_touches:
            add(raw, kind, proc_base)

        found.extend(resolved_here)
        if plan.chdir_unresolved:
            if not ignore_outside:
                raise PathOutsideWorkspaceError(UNRESOLVED_CHDIR_MSG)
            continue
        if plan.is_cd:
            updated = False
            for _relative, kind, path in resolved_here:
                if kind == "read":
                    cwd = path
                    updated = True
                    break
            if not updated and not ignore_outside:
                raise PathOutsideWorkspaceError(UNRESOLVED_CHDIR_MSG)
    return found, cwd


def _strip_compound(segment: str) -> tuple[str | None, str]:
    """``(...)`` 子 shell、``{...}`` 当前 shell 分组。否则原样返回。"""
    text = segment.strip()
    if len(text) >= 2 and text.startswith("(") and text.endswith(")"):
        return "subshell", text[1:-1]
    if len(text) >= 2 and text.startswith("{") and text.endswith("}"):
        return "group", text[1:-1].strip()
    return None, segment


def _try_resolve(
    workspace: Path, raw: str, base: Path, *, ignore_outside: bool
) -> tuple[str, Path] | None:
    """解析成功返回 ``(展示路径, 绝对路径)``；区外在 ``ignore_outside`` 时跳过。"""
    if _unresolved(raw):
        return None
    try:
        resolved = resolve_path(workspace, raw, base=base)
    except PathOutsideWorkspaceError:
        if ignore_outside:
            return None
        raise
    except Exception:
        return None
    return display(workspace, resolved), resolved


def _analyze_segment(segment: str, analysis: CommandPathAnalysis) -> None:
    kind, inner = _strip_compound(segment)
    if kind:
        for sub in split_command_segments(inner):
            _analyze_segment(sub, analysis)
        return
    plan = _plan_segment(segment)
    for raw, kind in plan.shell_touches:
        analysis.touches.append(PathTouch(raw=raw, kind=kind))
    if plan.proc_chdir:
        analysis.touches.append(PathTouch(raw=plan.proc_chdir, kind="read"))
    if plan.git_chdir:
        analysis.touches.append(PathTouch(raw=plan.git_chdir, kind="read"))
    for raw, kind in plan.inner_touches:
        analysis.touches.append(PathTouch(raw=raw, kind=kind))
    tokens = unwrap_argv(tokenize(segment)).tokens
    if tokens and executable_name(tokens[0]) in shell_catalog().env_dump_commands and _is_bare_env(tokens):
        analysis.dumps_env = True


@dataclass
class _SegmentPlan:
    """一段命令里：重定向跟 shell cwd，内层路径跟 ``env -C`` / ``git -C``。"""

    shell_touches: list[tuple[str, str]]
    proc_chdir: str | None
    git_chdir: str | None
    inner_touches: list[tuple[str, str]]
    is_cd: bool
    chdir_unresolved: bool = False


def _plan_segment(segment: str) -> _SegmentPlan:
    shell_touches: list[tuple[str, str]] = []
    for match in _REDIRECT.finditer(segment):
        op, target = match.group(1), match.group(2)
        if target.startswith("&"):
            continue
        shell_touches.append((target, "write" if ">" in op else "read"))

    tokens = tokenize(segment)
    unwrapped = unwrap_argv(tokens)
    inner = unwrapped.tokens
    head = executable_name(inner[0]) if inner else ""

    catalog = shell_catalog()
    if head in catalog.unresolved_chdir_commands:
        return _SegmentPlan(shell_touches, None, None, [], True, True)
    if head in catalog.chdir_commands:
        operands = [token for token in inner[1:] if not token.startswith("-") or token == "-"]
        if head == "pushd" and not operands:
            return _SegmentPlan(shell_touches, None, None, [], True, True)
        target = operands[-1] if operands else str(Path.home())
        shell_touches.append((target, "read"))
        return _SegmentPlan(
            shell_touches, None, None, [], True, _unresolved(target)
        )

    git_chdir: str | None = None
    inner_touches: list[tuple[str, str]] = []
    if inner and executable_name(inner[0]) == "git":
        peeled = peel_git_globals(inner)
        git_chdir = peeled.chdir
        inner_touches.extend(
            (path, kind) for path, kind in peeled.touches if path != peeled.chdir
        )
        inner_touches.extend((path, "read") for path in _git_content_paths(peeled.argv))
    elif inner:
        inner_touches.extend(_argv_touches(inner))
    return _SegmentPlan(shell_touches, unwrapped.chdir, git_chdir, inner_touches, False)


def _argv_touches(tokens: list[str]) -> list[tuple[str, str]]:
    """内层命令（已剥包装）碰到的路径。"""
    if not tokens:
        return []
    touches: list[tuple[str, str]] = []
    head = executable_name(tokens[0])
    touches.extend((path, "write") for path in _write_flag_paths(tokens))
    touches.extend((path, kind) for path, kind in _dd_paths(tokens))
    touches.extend((path, "read") for path in _upload_paths(tokens))

    catalog = shell_catalog()
    paths = _path_args(tokens)
    if head in catalog.write_all:
        touches.extend((path, "write") for path in paths)
    elif head in catalog.write_last and paths:
        touches.extend((path, "read") for path in paths[:-1])
        touches.append((paths[-1], "write"))
    elif head == "sed" and _sed_is_inplace(tokens):
        touches.extend((path, "write") for path in paths)
    elif head in catalog.inplace_interpreters and _inplace_rewrite(tokens):
        touches.extend((path, "write") for path in paths)
    elif head in catalog.read_args:
        touches.extend((path, "read") for path in paths)
    return touches


def _git_content_paths(argv: list[str]) -> list[str]:
    """``git show HEAD:.env`` / ``git blame id_rsa`` 这类能打出文件内容的参数。"""
    if not argv or argv[0] not in shell_catalog().git_content_subcommands:
        return []
    paths: list[str] = []
    skip_next = False
    for index, token in enumerate(argv[1:], start=1):
        if skip_next:
            skip_next = False
            continue
        if token == "--":
            for extra in argv[index + 1 :]:
                path = _git_path_from_token(extra, after_dash_dash=True)
                if path:
                    paths.append(path)
            break
        if token.startswith("-"):
            if token in {"-p", "--pretty", "-s", "--stat", "-w", "--color"}:
                continue
            skip_next = token in {"-t", "--type", "-o"} or (
                token.startswith("-") and not token.startswith("--") and len(token) == 2
            )
            continue
        path = _git_path_from_token(token, after_dash_dash=False)
        if path:
            paths.append(path)
    return paths


def _git_path_from_token(token: str, *, after_dash_dash: bool) -> str | None:
    if not token or token.startswith("-"):
        return None
    if ":" in token:
        suffix = token.rsplit(":", 1)[-1]
        return suffix or None
    if after_dash_dash or _looks_like_path(token):
        return token
    return None


def _looks_like_path(token: str) -> bool:
    name = token.rsplit("/", 1)[-1].lower()
    if token.startswith(".") or "/" in token or "\\" in token:
        return True
    return name in {
        ".env",
        ".envrc",
        ".npmrc",
        ".netrc",
        "credentials",
        "credentials.json",
        "id_rsa",
        "id_ed25519",
        "id_ecdsa",
        "authorized_keys",
        "kubeconfig",
    }


def _dd_paths(tokens: list[str]) -> list[tuple[str, str]]:
    """抽出 ``dd if=`` / ``of=``。"""
    if not tokens or executable_name(tokens[0]) != "dd":
        return []
    paths: list[tuple[str, str]] = []
    for token in tokens[1:]:
        if token.startswith("of=") and token[3:]:
            paths.append((token[3:], "write"))
        elif token.startswith("if=") and token[3:]:
            paths.append((token[3:], "read"))
    return paths


def _upload_paths(tokens: list[str]) -> list[str]:
    """抽出 ``curl -d @file`` / ``wget --post-file`` 读到的本地文件。"""
    if not tokens:
        return []
    head = executable_name(tokens[0])
    catalog = shell_catalog()
    next_flags = catalog.upload_flag_next.get(head)
    eq_prefixes = catalog.upload_flag_eq.get(head, ())
    if not next_flags and not eq_prefixes:
        return []
    paths: list[str] = []
    skip_next = False
    always_path_flags = catalog.upload_always_path_flags
    for index, token in enumerate(tokens[1:], start=1):
        if skip_next:
            skip_next = False
            continue
        if next_flags and token in next_flags:
            if index + 1 < len(tokens):
                paths.extend(_upload_target(tokens[index + 1], always_path=token in always_path_flags))
                skip_next = True
            continue
        for prefix in eq_prefixes:
            if token.startswith(prefix):
                flag = prefix.rstrip("=")
                paths.extend(
                    _upload_target(token[len(prefix) :], always_path=flag in always_path_flags)
                )
                break
    return paths


def _upload_target(raw: str, *, always_path: bool) -> list[str]:
    if not raw:
        return []
    if raw.startswith("@"):
        path = raw[1:]
        return [path] if path else []
    return [raw] if always_path else []


def _inplace_rewrite(tokens: list[str]) -> bool:
    """``perl -i`` / ``ruby -i`` / ``php -i`` 会原地改文件。"""
    for token in tokens[1:]:
        if token == "--":
            break
        if token in {"-i", "--inplace"} or token.startswith("-i") or token.startswith("--inplace="):
            return True
    return False


def _write_flag_paths(tokens: list[str]) -> list[str]:
    """抽出 ``curl -o FILE`` / ``wget -O FILE`` / ``sort -o FILE`` 的写目标。"""
    if not tokens:
        return []
    head = executable_name(tokens[0])
    catalog = shell_catalog()
    next_flags = catalog.write_flag_next.get(head)
    eq_prefixes = catalog.write_flag_eq.get(head, ())
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
    flags = shell_catalog().sed_inplace_flags
    for token in tokens[1:]:
        if token == "--":
            break
        if token in flags or token.startswith("-i") or token.startswith("--in-place="):
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


def _is_bare_env(tokens: list[str]) -> bool:
    name = executable_name(tokens[0])
    catalog = shell_catalog()
    if name not in catalog.env_dump_commands:
        return False
    if name != "env":
        return True
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


def _is_pytest_module(tokens: list[str]) -> bool:
    return executable_name(tokens[0]) in shell_catalog().python_binaries and tokens[1:3] == ["-m", "pytest"]


def _is_opaque(command: str) -> bool:
    if any(pattern.search(command) for pattern in _OPAQUE_PATTERNS):
        return True
    catalog = shell_catalog()
    for segment in split_command_segments(command):
        kind, inner = _strip_compound(segment)
        if kind:
            if _is_opaque(inner):
                return True
            continue
        tokens = unwrap_argv(tokenize(segment)).tokens
        if not tokens:
            continue
        head = executable_name(tokens[0])
        if head in catalog.opaque_heads:
            if head in catalog.stdin_shells and len(tokens) == 1:
                continue
            return True
        if head in catalog.interpreters and not _is_pytest_module(tokens):
            return True
    return False


_QUOTED = re.compile(r"'[^']*'|\"[^\"]*\"")


def _is_unbounded_read(command: str) -> bool:
    """通配或递归搜索可能扫到 .env / 密钥，不能当「看清了路径的只读」。"""
    catalog = shell_catalog()
    for segment in split_command_segments(command):
        kind, inner = _strip_compound(segment)
        if kind:
            if _is_unbounded_read(inner):
                return True
            continue
        tokens = unwrap_argv(tokenize(segment)).tokens
        if not tokens:
            continue
        if executable_name(tokens[0]) == "git":
            peeled = peel_git_globals(tokens)
            if peeled.argv and peeled.argv[0] == "grep":
                return True
        head, *rest = tokens
        if executable_name(head) in catalog.recursive_search_heads:
            return True
        if executable_name(head) in catalog.grep_heads and _has_recursive_grep_flag(rest):
            return True
        unquoted = _QUOTED.sub(" ", segment)
        unquoted_tokens = tokenize(unquoted)
        if any(_is_glob_token(token) for token in unquoted_tokens[1:]):
            return True
    return False


def _has_recursive_grep_flag(tokens: list[str]) -> bool:
    flags = shell_catalog().recursive_grep_flags
    for token in tokens:
        if token in flags:
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
