"""Shell 命令分析：Plan 模式只读判定，以及执行头提取（白名单共用）。"""

from __future__ import annotations

import re
import shlex

# bash 把换行和 ; 同级当命令分隔符。只切 && || ; | 时，
# ``cd /tmp\\necho x > a`` 会当成一段，相对路径按工作区解析，实际写到 /tmp。
_SPLIT_OPERATORS = re.compile(r"(\|\||&&|;|\||\r\n|\n|\r)")
_SEPARATOR_TOKENS = frozenset({"||", "&&", ";", "|", "\r\n", "\n", "\r"})

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

# git 子命令：本身就是只读操作，不需要再看参数。
_GIT_READONLY_SUBCOMMANDS = frozenset(
    {
        "status", "diff", "log", "show", "blame", "describe", "shortlog",
        "reflog", "grep", "ls-files", "ls-tree", "rev-parse", "cat-file",
        "show-ref", "for-each-ref", "diff-tree",
    }
)


def split_command_segments(command: str) -> list[str]:
    """按 ``&&`` / ``||`` / ``;`` / ``|`` / 换行切开，去掉分隔符本身。"""
    return [
        seg.strip()
        for seg in _SPLIT_OPERATORS.split(command)
        if seg.strip() and seg not in _SEPARATOR_TOKENS
    ]


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
    """命令的执行头：第一个 token；``python -m pytest`` / ``git status`` 视为一个整体。"""
    try:
        tokens = shlex.split(command)
    except ValueError:
        tokens = command.split()
    if not tokens:
        return ""
    if tokens[0] in {"python", "python3"} and tokens[1:3] == ["-m", "pytest"]:
        return "python -m pytest"
    if tokens[0] == "git":
        for token in tokens[1:]:
            if not token.startswith("-"):
                return f"git {token}"
        return "git"
    return tokens[0]


def _is_readonly_segment(segment: str) -> bool:
    if "`" in segment or "$(" in segment:
        return False  # 命令替换可能嵌套任意命令，无法保证只读，保守拒绝
    if re.search(r">(?!&)", segment):
        return False  # 文件写重定向（> / >> / 2> file）；2>&1 这种 fd 合并允许

    try:
        tokens = shlex.split(segment)
    except ValueError:
        return False
    if not tokens:
        return False
    if "&" in tokens:
        return False  # 后台执行，脱离了本次判定能追踪的范围

    head, *rest = tokens
    if head == "git":
        return _is_readonly_git(rest)
    if head == "env":
        return _is_readonly_env(rest)
    if command_head(segment) == "python -m pytest":
        return True
    if head == "find":
        return not any(flag in _FIND_DANGEROUS_FLAGS for flag in rest)
    if head == "sed":
        return not any(flag in _SED_INPLACE_FLAGS for flag in rest)
    return head in _SIMPLE_READONLY_BINARIES


def _is_readonly_env(rest: list[str]) -> bool:
    """``env`` 本身只打印环境变量；一旦后面跟了要执行的命令，就按那条命令再判定。

    否则 ``env FOO=1 touch x`` 会被当成只读白名单命令，Plan 模式下直接放行。
    """
    i = 0
    while i < len(rest):
        arg = rest[i]
        if arg == "--":
            return _is_readonly_argv(rest[i + 1 :]) if rest[i + 1 :] else True
        if arg in {"-u", "--unset", "-C", "--chdir"}:
            i += 2
            continue
        if arg.startswith("-") and not arg.startswith("-="):
            i += 1
            continue
        if "=" in arg:
            i += 1
            continue
        return _is_readonly_argv(rest[i:])
    return True  # 裸 `env` / 只有赋值，相当于打印环境变量


def _is_readonly_argv(tokens: list[str]) -> bool:
    if not tokens:
        return False
    fake = " ".join(shlex.quote(t) for t in tokens)
    return _is_readonly_segment(fake)


def _is_readonly_git(rest: list[str]) -> bool:
    if not rest:
        return False  # 裸 `git` 没有子命令，没有意义，保守拒绝
    subcommand, *args = rest
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
