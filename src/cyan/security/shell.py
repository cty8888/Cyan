"""Shell 命令分析：切段、包装展开、Plan 只读判定、执行头提取。

白名单、只读、路径抽取共用这里的切段和展开结果：
一段命令先剥 ``env`` / ``timeout`` 等前缀，再认 ``git -C`` 这类全局选项，
最后才看真正的命令头。复合命令按 ``&&`` / ``||`` / ``;`` / ``|`` / ``|&`` / ``&`` / 换行切段，
每段各自判定，不能拿第一段的头去放行整串。
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from pathlib import Path

from .catalog import shell_catalog

# bash 把换行和 ; 同级当命令分隔符。只切 && || ; | 时，
# ``cd /tmp\\necho x > a`` 会当成一段，相对路径按工作区解析，实际写到 /tmp。
# 引号内的分隔符不切，否则 ``git commit -m 'fix; extra'`` 会拆出假命令头。
_TWO_CHAR_SEPARATORS = ("||", "&&", "|&", "\r\n")
_ONE_CHAR_SEPARATORS = frozenset({";", "|", "\n", "\r"})

# 只读命令 / 包装前缀 / 路径分析命令名：见 defaults.json ``shell``。
# 下面这些是剥包装、git 全局选项的参数形状，和具体命令名单无关。
_ENV_VALUE_FLAGS = frozenset({"-u", "--unset", "-C", "--chdir", "-S", "--split-string"})
_TIMEOUT_VALUE_FLAGS = frozenset({"-k", "--kill-after", "-s", "--signal"})
_STDBUF_VALUE_FLAGS = frozenset({"-i", "-o", "-e"})
_GIT_CHDIR_FLAGS = frozenset({"-C", "--work-tree"})
_GIT_DIR_FLAGS = frozenset({"--git-dir"})
_GIT_CONFIG_FLAGS = frozenset({"-c", "--namespace", "--config-env", "--super-prefix", "--exec-path"})
_GIT_CHDIR_EQ = ("--work-tree=",)
_GIT_DIR_EQ = ("--git-dir=",)
_GIT_CONFIG_EQ = ("--namespace=", "--config-env=", "--super-prefix=", "--exec-path=")
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


@dataclass(frozen=True)
class UnwrappedArgv:
    """剥掉 ``env`` / ``timeout`` 等前缀之后的 argv。

    ``chdir`` 是 ``env -C`` / ``--chdir`` 留下的进程工作目录，相对外层 shell cwd。
    """

    tokens: list[str]
    chdir: str | None = None


@dataclass(frozen=True)
class GitGlobals:
    """``git`` 全局选项剥完之后剩下的子命令 argv。"""

    argv: list[str]
    chdir: str | None = None
    touches: tuple[tuple[str, str], ...] = ()


def split_command_segments(command: str) -> list[str]:
    """按 ``&&`` / ``||`` / ``;`` / ``|`` / ``|&`` / ``&`` / 换行切开，去掉分隔符本身。

    单引号 / 双引号里的分隔符不切。双引号里 ``\\"`` 不算结束引号。
    ``2>&1`` / ``&>file`` 这类重定向里的 ``&`` 不切。
    ``(...)`` / ``{...}`` 里的分隔符也不切，交给路径分析按子 shell / 分组处理。
    """
    segments: list[str] = []
    buf: list[str] = []
    quote: str | None = None
    index = 0
    length = len(command)

    def flush() -> None:
        text = "".join(buf).strip()
        buf.clear()
        if text:
            segments.append(text)

    paren = 0
    brace = 0
    while index < length:
        char = command[index]
        if quote == "'":
            buf.append(char)
            if char == "'":
                quote = None
            index += 1
            continue
        if quote == '"':
            buf.append(char)
            if char == '"' and not _is_escaped(command, index):
                quote = None
            index += 1
            continue
        if char in {"'", '"'}:
            quote = char
            buf.append(char)
            index += 1
            continue
        if char == "(":
            paren += 1
            buf.append(char)
            index += 1
            continue
        if char == ")" and paren:
            paren -= 1
            buf.append(char)
            index += 1
            continue
        if char == "{":
            brace += 1
            buf.append(char)
            index += 1
            continue
        if char == "}" and brace:
            brace -= 1
            buf.append(char)
            index += 1
            continue
        if paren or brace:
            buf.append(char)
            index += 1
            continue
        matched = next((sep for sep in _TWO_CHAR_SEPARATORS if command.startswith(sep, index)), None)
        if matched is not None:
            flush()
            index += len(matched)
            continue
        if char == "&":
            prev = command[index - 1] if index else ""
            nxt = command[index + 1] if index + 1 < length else ""
            if prev in "<>" or nxt in "<>":
                buf.append(char)
                index += 1
                continue
            flush()
            index += 1
            continue
        if char in _ONE_CHAR_SEPARATORS:
            flush()
            index += 1
            continue
        buf.append(char)
        index += 1
    flush()
    return segments


def _is_escaped(text: str, index: int) -> bool:
    """``index`` 处字符前面有奇数个反斜杠则为转义。"""
    slashes = 0
    cursor = index - 1
    while cursor >= 0 and text[cursor] == "\\":
        slashes += 1
        cursor -= 1
    return slashes % 2 == 1


def iter_command_substitutions(command: str) -> list[str]:
    """抽出 ``$(...)`` / `` `...` `` / ``<(...)`` / ``>(...)`` 的内层命令（含嵌套）。"""
    found: list[str] = []
    _scan_substitutions(command, found)
    return found


def _scan_substitutions(text: str, found: list[str]) -> None:
    index = 0
    length = len(text)
    while index < length:
        if text.startswith("$(", index) or text.startswith("<(", index) or text.startswith(">(", index):
            inner, end = _extract_paren_inner(text, index + 2)
            if inner is None:
                index += 1
                continue
            found.append(inner)
            _scan_substitutions(inner, found)
            index = end
            continue
        if text[index] == "`":
            close = text.find("`", index + 1)
            if close == -1:
                break
            inner = text[index + 1 : close]
            found.append(inner)
            _scan_substitutions(inner, found)
            index = close + 1
            continue
        index += 1


def _extract_paren_inner(text: str, start: int) -> tuple[str | None, int]:
    depth = 1
    index = start
    in_single = False
    in_double = False
    while index < len(text):
        char = text[index]
        if in_single:
            if char == "'":
                in_single = False
            index += 1
            continue
        if in_double:
            if char == "\\" and index + 1 < len(text):
                index += 2
                continue
            if char == '"':
                in_double = False
            index += 1
            continue
        if char == "'":
            in_single = True
        elif char == '"':
            in_double = True
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return text[start:index], index + 1
        index += 1
    return None, start


def tokenize(segment: str) -> list[str]:
    """把一段命令拆成 argv；引号不配对时退回空白切分。"""
    try:
        return shlex.split(segment)
    except ValueError:
        return segment.split()


def executable_name(token: str) -> str:
    """``/usr/bin/env`` → ``env``，供包装命令识别。

    ``.`` / ``..`` 在 pathlib 里 ``name`` 是空串，必须原样保留，否则点命令
    不会被当成 source 同类的不透明头。
    """
    if token in {".", ".."}:
        return token
    name = Path(token).name
    return name or token


def unwrap_argv(tokens: list[str]) -> UnwrappedArgv:
    """剥 ``env`` / ``timeout`` / ``nice`` / ``exec`` / ``builtin`` 等前缀，露出真正要执行的命令。

    剥不干净（裸 ``env``、缺 duration 的 ``timeout``）就停，tokens 保持原样。
    """
    current = list(tokens)
    chdir: str | None = None
    unwrap = shell_catalog().unwrap
    while current:
        name = executable_name(current[0])
        kind = unwrap.get(name)
        if kind == "env":
            inner, env_chdir = _peel_env(current)
            if env_chdir:
                chdir = env_chdir
            if inner is None:
                return UnwrappedArgv(current, chdir)
            current = inner
            continue
        if kind == "timeout":
            inner = _peel_timeout(current)
            if inner is None:
                break
            current = inner
            continue
        if kind == "nice":
            inner = _peel_nice(current)
            if inner is None:
                break
            current = inner
            continue
        if kind == "stdbuf":
            inner = _peel_stdbuf(current)
            if inner is None:
                break
            current = inner
            continue
        if kind == "command":
            inner = _peel_command_wrapper(current)
            if inner is None:
                break
            current = inner
            continue
        if kind == "exec":
            inner = _peel_exec(current)
            if inner is None:
                break
            current = inner
            continue
        if kind == "passthrough":
            inner = current[1:]
            if not inner:
                break
            current = inner
            continue
        break
    return UnwrappedArgv(current, chdir)


def peel_leading_assignments(tokens: list[str], *, all_assignments: bool) -> list[str]:
    """剥开头的 ``VAR=value``。

    deny/ask 剥掉任意赋值再匹配；allow 只剥已知安全的变量，其余前缀留在命令里，
    因此 ``FOO=bar pytest`` 对不上 ``bash(pytest *)``。
    """
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token.startswith("-") or "=" not in token:
            break
        name, _, _ = token.partition("=")
        if not _ENV_NAME.fullmatch(name):
            break
        if not all_assignments and name not in shell_catalog().safe_env_names:
            break
        index += 1
    return tokens[index:]


def is_accept_edits_fs_command(command: str) -> bool:
    """AcceptEdits 自动放行的工作区内文件系统命令：mkdir / touch / rm / mv / cp / sed。"""
    segments = split_command_segments(command)
    if not segments:
        return False
    for segment in segments:
        if "`" in segment or "$(" in segment or "<(" in segment or ">(" in segment:
            return False
        tokens = peel_leading_assignments(
            unwrap_argv(tokenize(segment)).tokens, all_assignments=False
        )
        if not tokens:
            return False
        if executable_name(tokens[0]) not in shell_catalog().accept_edits_fs:
            return False
    return True


def peel_git_globals(tokens: list[str]) -> GitGlobals:
    """剥 ``git -C`` / ``--git-dir`` / ``--work-tree`` / ``-c``，露出子命令。"""
    if not tokens or executable_name(tokens[0]) != "git":
        return GitGlobals(argv=list(tokens))

    chdir: str | None = None
    touches: list[tuple[str, str]] = []
    index = 1
    while index < len(tokens):
        token = tokens[index]
        if token == "--":
            index += 1
            break
        if token in _GIT_CHDIR_FLAGS:
            if index + 1 >= len(tokens):
                break
            value = tokens[index + 1]
            chdir = value
            touches.append((value, "read"))
            index += 2
            continue
        if token in _GIT_DIR_FLAGS:
            if index + 1 >= len(tokens):
                break
            touches.append((tokens[index + 1], "write"))
            index += 2
            continue
        if token in _GIT_CONFIG_FLAGS:
            index += 2 if index + 1 < len(tokens) else 1
            continue
        matched_eq = False
        for prefix in _GIT_CHDIR_EQ:
            if token.startswith(prefix):
                value = token[len(prefix) :]
                if value:
                    chdir = value
                    touches.append((value, "read"))
                matched_eq = True
                break
        if matched_eq:
            index += 1
            continue
        for prefix in _GIT_DIR_EQ:
            if token.startswith(prefix):
                value = token[len(prefix) :]
                if value:
                    touches.append((value, "write"))
                matched_eq = True
                break
        if matched_eq:
            index += 1
            continue
        for prefix in _GIT_CONFIG_EQ:
            if token.startswith(prefix):
                matched_eq = True
                break
        if matched_eq:
            index += 1
            continue
        if token.startswith("-"):
            index += 1
            continue
        break
    return GitGlobals(argv=tokens[index:], chdir=chdir, touches=tuple(touches))


def is_readonly_command(command: str) -> bool:
    """Plan 模式：判断 shell 命令是否为只读操作。

    保守启发式：只要有一段无法确认是只读的，整条命令就判定为「不是只读」——
    宁可多问用户一次确认，也不要在 Plan 模式下悄悄放过一个会改动状态的命令。
    """
    if not command.strip():
        return False

    segments = split_command_segments(command)
    if not segments:
        return False
    return all(_is_readonly_segment(seg) for seg in segments)


def command_head(command: str) -> str:
    """一段命令的执行头。先展开包装，``python -m pytest`` / ``git status`` 视为一个整体。

    复合命令应先 ``split_command_segments`` 再逐段调用；对本函数传入整串时，
    ``&&`` 只是普通 token，结果仍是第一段的头——白名单不能依赖这一点。
    """
    tokens = unwrap_argv(tokenize(command)).tokens
    if not tokens:
        return ""
    if tokens[0] in shell_catalog().python_binaries and tokens[1:3] == ["-m", "pytest"]:
        return "python -m pytest"
    if executable_name(tokens[0]) == "git":
        peeled = peel_git_globals(tokens)
        if not peeled.argv:
            return "git"
        return f"git {peeled.argv[0]}"
    return tokens[0]


def command_heads(command: str) -> list[str]:
    """复合命令里每一段的执行头，供白名单按段核对。"""
    heads: list[str] = []
    for segment in split_command_segments(command):
        head = command_head(segment)
        if head:
            heads.append(head)
    return heads


def _is_readonly_segment(segment: str) -> bool:
    if "`" in segment or "$(" in segment or "<(" in segment or ">(" in segment:
        return False  # 命令替换 / 进程替换可能嵌套任意命令，无法保证只读
    if re.search(r">(?!&)", segment):
        return False  # 文件写重定向（> / >> / 2> file）；2>&1 这种 fd 合并允许

    tokens = tokenize(segment)
    if not tokens:
        return False
    if "&" in tokens:
        return False  # 后台执行，脱离了本次判定能追踪的范围

    unwrapped = unwrap_argv(tokens)
    tokens = peel_leading_assignments(unwrapped.tokens, all_assignments=False)
    if not tokens:
        return False

    head, *rest = tokens
    name = executable_name(head)
    if name == "git":
        return _is_readonly_git(rest)
    if name == "env":
        return _is_readonly_env(rest)
    if command_head(segment) == "python -m pytest":
        return True
    if name == "find":
        return not any(flag in shell_catalog().find_dangerous_flags for flag in rest)
    if name == "sed":
        flags = shell_catalog().sed_inplace_flags
        return not any(flag in flags or flag.startswith("-i") for flag in rest)
    if name == "sort":
        return not _sort_writes(rest)
    return name in shell_catalog().readonly_binaries


def _sort_writes(rest: list[str]) -> bool:
    """``sort -o FILE`` / ``--output=FILE`` 会写文件，不能当只读。"""
    flags = shell_catalog().sort_output_flags
    for token in rest:
        if token in flags or token.startswith("--output="):
            return True
    return False


def _peel_env(tokens: list[str]) -> tuple[list[str] | None, str | None]:
    """返回 ``(内层命令, -C 目录)``。裸 ``env`` 的内层是 ``None``。"""
    chdir: str | None = None
    index = 1
    while index < len(tokens):
        arg = tokens[index]
        if arg == "--":
            inner = tokens[index + 1 :]
            return (inner or None, chdir)
        if arg in {"-C", "--chdir"}:
            if index + 1 >= len(tokens):
                return None, chdir
            chdir = tokens[index + 1]
            index += 2
            continue
        if arg.startswith("--chdir="):
            chdir = arg[len("--chdir=") :]
            index += 1
            continue
        if arg in _ENV_VALUE_FLAGS:
            index += 2
            continue
        if arg.startswith("-") and not arg.startswith("-="):
            index += 1
            continue
        if "=" in arg:
            index += 1
            continue
        return tokens[index:], chdir
    return None, chdir


def _peel_timeout(tokens: list[str]) -> list[str] | None:
    index = 1
    while index < len(tokens):
        arg = tokens[index]
        if arg == "--":
            inner = tokens[index + 1 :]
            return inner or None
        if arg in _TIMEOUT_VALUE_FLAGS:
            index += 2
            continue
        if arg.startswith("--kill-after=") or arg.startswith("--signal="):
            index += 1
            continue
        if arg.startswith("-"):
            index += 1
            continue
        # 下一个非选项是 duration，再后面才是命令
        inner = tokens[index + 1 :]
        return inner or None
    return None


def _peel_nice(tokens: list[str]) -> list[str] | None:
    rest = tokens[1:]
    if not rest:
        return None
    if rest[0] in {"-n", "--adjustment"}:
        inner = rest[2:]
        return inner or None
    if rest[0].startswith("-") and rest[0][1:].lstrip("+-").isdigit():
        inner = rest[1:]
        return inner or None
    return rest


def _peel_stdbuf(tokens: list[str]) -> list[str] | None:
    index = 1
    while index < len(tokens):
        arg = tokens[index]
        if arg == "--":
            inner = tokens[index + 1 :]
            return inner or None
        if arg in _STDBUF_VALUE_FLAGS:
            index += 2
            continue
        if arg.startswith("-"):
            index += 1
            continue
        return tokens[index:]
    return None


def _peel_exec(tokens: list[str]) -> list[str] | None:
    """``exec [-cl] [-a name] command``：露出真正要执行的命令。"""
    index = 1
    while index < len(tokens):
        arg = tokens[index]
        if arg == "--":
            inner = tokens[index + 1 :]
            return inner or None
        if arg in {"-c", "-l"}:
            index += 1
            continue
        if arg == "-a":
            index += 2
            continue
        if arg.startswith("-"):
            index += 1
            continue
        return tokens[index:]
    return None


def _peel_command_wrapper(tokens: list[str]) -> list[str] | None:
    index = 1
    while index < len(tokens):
        arg = tokens[index]
        if arg == "--":
            inner = tokens[index + 1 :]
            return inner or None
        if arg in {"-p", "-v", "-V"}:
            index += 1
            continue
        if arg.startswith("-"):
            index += 1
            continue
        return tokens[index:]
    return None


def _is_readonly_env(rest: list[str]) -> bool:
    """``env`` 本身只打印环境变量；一旦后面跟了要执行的命令，就按那条命令再判定。

    否则 ``env FOO=1 touch x`` 会被当成只读白名单命令，Plan 模式下直接放行。
    """
    inner, _chdir = _peel_env(["env", *rest])
    if inner is None:
        return True
    return _is_readonly_argv(inner)


def _is_readonly_argv(tokens: list[str]) -> bool:
    if not tokens:
        return False
    fake = " ".join(shlex.quote(t) for t in tokens)
    return _is_readonly_segment(fake)


def _is_readonly_git(rest: list[str]) -> bool:
    peeled = peel_git_globals(["git", *rest])
    if not peeled.argv:
        return False  # 裸 `git` 没有子命令，没有意义，保守拒绝
    subcommand, *args = peeled.argv
    if subcommand in shell_catalog().git_readonly_subcommands:
        return True
    handler = _GIT_CONDITIONAL_READONLY.get(subcommand)
    return handler(args) if handler else False


def _readonly_branch(args: list[str]) -> bool:
    allowed = {"-a", "-v", "-vv", "-r", "--list", "--show-current", "--all", "--remote"}
    return all(arg in allowed for arg in args)


def _readonly_tag(args: list[str]) -> bool:
    # `git tag` 不带任何非 flag 参数是列出所有 tag；带一个名字就是创建 tag。
    return not any(not arg.startswith("-") for arg in args)


def _readonly_remote(args: list[str]) -> bool:
    if not args:
        return True  # 裸 `git remote` 列出远端名字
    return args[0] in {"-v", "--verbose", "show", "get-url"}


def _readonly_config(args: list[str]) -> bool:
    return any(arg in {"--get", "--list", "-l", "--get-all", "--get-regexp"} for arg in args)


def _readonly_stash(args: list[str]) -> bool:
    return bool(args) and args[0] in {"list", "show"}


_GIT_CONDITIONAL_READONLY = {
    "branch": _readonly_branch,
    "tag": _readonly_tag,
    "remote": _readonly_remote,
    "config": _readonly_config,
    "stash": _readonly_stash,
}
