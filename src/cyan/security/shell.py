"""Shell 命令分析：切段、包装展开、Plan 只读判定、执行头提取。

白名单、只读、路径抽取共用这里的切段和展开结果：
一段命令先剥 ``env`` / ``timeout`` 等前缀，再认 ``git -C`` 这类全局选项，
最后才看真正的命令头。复合命令按 ``&&`` / ``||`` / ``;`` / ``|`` / 换行切段，
每段各自判定，不能拿第一段的头去放行整串。
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from pathlib import Path

# bash 把换行和 ; 同级当命令分隔符。只切 && || ; | 时，
# ``cd /tmp\\necho x > a`` 会当成一段，相对路径按工作区解析，实际写到 /tmp。
# 引号内的分隔符不切，否则 ``git commit -m 'fix; extra'`` 会拆出假命令头。
_TWO_CHAR_SEPARATORS = ("||", "&&", "\r\n")
_ONE_CHAR_SEPARATORS = frozenset({";", "|", "\n", "\r"})

# 不带子命令歧义、单看命令名就能判定"只读"的可执行文件。
# pytest 显式列在其中：README/PLAN_EXEC_MSG 都拿它当"Plan 模式允许的典型例子"，
# 即便它可能顺手写一点 __pycache__/.pytest_cache 这类无关缓存，也不算"改动用户代码"。
_SIMPLE_READONLY_BINARIES = frozenset(
    {
        "ls", "pwd", "echo", "cat", "head", "tail", "wc", "grep", "egrep",
        "fgrep", "rg", "ag", "diff", "file", "stat", "du", "df", "tree",
        "which", "type", "printenv", "date", "whoami", "id", "uname",
        "ps", "sort", "uniq", "cut", "tr", "column", "nl", "less", "more",
        "jq", "pytest", "md5sum", "sha1sum", "sha256sum",
    }
)

_FIND_DANGEROUS_FLAGS = frozenset({"-delete", "-exec", "-execdir", "-fprintf", "-fls", "-ok", "-okdir"})
_SED_INPLACE_FLAGS = frozenset({"-i", "--in-place"})
_SORT_OUTPUT_FLAGS = frozenset({"-o", "--output"})

# git 子命令：本身就是只读操作，不需要再看参数。
_GIT_READONLY_SUBCOMMANDS = frozenset(
    {
        "status", "diff", "log", "show", "blame", "describe", "shortlog",
        "reflog", "grep", "ls-files", "ls-tree", "rev-parse", "cat-file",
        "show-ref", "for-each-ref", "diff-tree",
    }
)

_ENV_VALUE_FLAGS = frozenset({"-u", "--unset", "-C", "--chdir", "-S", "--split-string"})
_TIMEOUT_VALUE_FLAGS = frozenset({"-k", "--kill-after", "-s", "--signal"})
_STDBUF_VALUE_FLAGS = frozenset({"-i", "-o", "-e"})
_GIT_CHDIR_FLAGS = frozenset({"-C", "--work-tree"})
_GIT_DIR_FLAGS = frozenset({"--git-dir"})
_GIT_CONFIG_FLAGS = frozenset({"-c", "--namespace", "--config-env", "--super-prefix", "--exec-path"})
_GIT_CHDIR_EQ = ("--work-tree=",)
_GIT_DIR_EQ = ("--git-dir=",)
_GIT_CONFIG_EQ = ("--namespace=", "--config-env=", "--super-prefix=", "--exec-path=")


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
    """按 ``&&`` / ``||`` / ``;`` / ``|`` / 换行切开，去掉分隔符本身。

    单引号 / 双引号里的分隔符不切。双引号里 ``\\"`` 不算结束引号。
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
        matched = next((sep for sep in _TWO_CHAR_SEPARATORS if command.startswith(sep, index)), None)
        if matched is not None:
            flush()
            index += len(matched)
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
    """剥 ``env`` / ``timeout`` / ``nice`` 等前缀，露出真正要执行的命令。

    剥不干净（裸 ``env``、缺 duration 的 ``timeout``）就停，tokens 保持原样。
    """
    current = list(tokens)
    chdir: str | None = None
    while current:
        name = executable_name(current[0])
        if name == "env":
            inner, env_chdir = _peel_env(current)
            if env_chdir:
                chdir = env_chdir
            if inner is None:
                return UnwrappedArgv(current, chdir)
            current = inner
            continue
        if name == "timeout":
            inner = _peel_timeout(current)
            if inner is None:
                break
            current = inner
            continue
        if name == "nice":
            inner = _peel_nice(current)
            if inner is None:
                break
            current = inner
            continue
        if name == "stdbuf":
            inner = _peel_stdbuf(current)
            if inner is None:
                break
            current = inner
            continue
        if name == "command":
            inner = _peel_command_wrapper(current)
            if inner is None:
                break
            current = inner
            continue
        if name in {"nohup", "time"}:
            inner = current[1:]
            if not inner:
                break
            current = inner
            continue
        break
    return UnwrappedArgv(current, chdir)


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
    if tokens[0] in {"python", "python3"} and tokens[1:3] == ["-m", "pytest"]:
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
    tokens = unwrapped.tokens
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
        return not any(flag in _FIND_DANGEROUS_FLAGS for flag in rest)
    if name == "sed":
        return not any(flag in _SED_INPLACE_FLAGS or flag.startswith("-i") for flag in rest)
    if name == "sort":
        return not _sort_writes(rest)
    return name in _SIMPLE_READONLY_BINARIES


def _sort_writes(rest: list[str]) -> bool:
    """``sort -o FILE`` / ``--output=FILE`` 会写文件，不能当只读。"""
    for token in rest:
        if token in _SORT_OUTPUT_FLAGS or token.startswith("--output="):
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
    if subcommand in _GIT_READONLY_SUBCOMMANDS:
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
