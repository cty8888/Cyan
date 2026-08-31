"""权限规则语法：``工具`` 或 ``工具(指定符)``。

对外名称对齐 Claude Code：``Bash`` / ``Read`` / ``Edit``。
``Edit`` 覆盖所有写入工具（write_file 与 edit_file）。
``Write`` 裸名仍按写入工具匹配；``Write(路径)`` 接受但不参与路径检查。
``Tool(param:value)`` 按顶级输入参数匹配（deny / ask）；``WebFetch(domain:host)``
是网络工具自己的指定符，allow 也可以用，方便以后加抓取工具。
未知工具名按工具名本身匹配，不必先改解析器。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

from .shell import peel_leading_assignments, split_command_segments, tokenize, unwrap_argv

PathAnchor = Literal["relative", "settings", "home", "abs"]

FAMILIES = frozenset({"bash", "read", "write"})
FAMILY_DISPLAY = {"bash": "Bash", "read": "Read", "write": "Edit"}
_NAME_TO_FAMILY = {
    "bash": "bash",
    "read": "read",
    "write": "write",
    "edit": "write",
}
_NAME_TO_TOOL = {
    "bash": "bash",
    "read": "read_file",
    "write": "write_file",
    "edit": "edit_file",
    "glob": "glob",
    "grep": "grep",
    "list_dir": "list_dir",
    "listdir": "list_dir",
    "webfetch": "webfetch",
}
# 主要内容字段不能走 param:value，否则复合命令 / 路径规则会被绕过。
_PRIMARY_FIELDS = {
    "bash": frozenset({"command"}),
    "read": frozenset({"path", "file_path"}),
    "write": frozenset({"path", "file_path"}),
    "edit": frozenset({"path", "file_path"}),
    "glob": frozenset({"path"}),
    "grep": frozenset({"path"}),
    "list_dir": frozenset({"path"}),
    "listdir": frozenset({"path"}),
    "webfetch": frozenset({"url"}),
}
_PARAM_SPEC = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\s*:\s*(.*)$", re.DOTALL)
_TOOL_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


@dataclass(frozen=True)
class ParsedRule:
    """一条已解析的规则。``pattern is None`` 表示匹配该工具的全部用法。

    ``inert``：``Write(路径)`` 或主要内容上的 ``param:value``，收下但不匹配。
    ``param``：``Tool(param:value)`` 的参数名；``domain`` 是网络工具指定符。
    """

    family: str
    pattern: str | None
    raw: str
    inert: bool = False
    param: str | None = None
    tool_name: str | None = None


def parse_rule(raw: str) -> ParsedRule:
    """解析 ``Bash(pytest *)`` / ``Read(.env)`` / ``Edit`` / ``WebFetch(domain:host)``。

    ``Write`` 裸名匹配写入工具；``Write(src/**)`` 会解析但 ``inert=True``。
    ``Bash(command:…)`` 同类主要内容参数会 ``inert=True``。
    """
    text = raw.strip()
    if not text:
        raise ValueError("规则不能为空")
    if "(" in text:
        if not text.endswith(")"):
            raise ValueError(f"规则括号未闭合：{raw!r}")
        name, _, rest = text.partition("(")
        label = name.strip()
        name_key = label.lower()
        if not _TOOL_NAME.fullmatch(label):
            raise ValueError(f"未知规则名 {label!r}，可选：Bash / Read / Edit，或 Tool(param:value)")
        inner = rest[:-1].strip()
        family = _NAME_TO_FAMILY.get(name_key, name_key)
        tool_name = _NAME_TO_TOOL.get(name_key, name_key)
        param_hit = _PARAM_SPEC.fullmatch(inner) if inner else None
        if param_hit is not None:
            param = param_hit.group(1).lower()
            value = param_hit.group(2)
            inert = param in _PRIMARY_FIELDS.get(name_key, ())
            return ParsedRule(
                family=family,
                pattern=value,
                raw=text,
                inert=inert,
                param=param,
                tool_name=tool_name,
            )
        inert = name_key == "write" and bool(inner)
        named = None if name_key in _NAME_TO_FAMILY else tool_name
        return ParsedRule(
            family=family, pattern=inner or None, raw=text, inert=inert, tool_name=named
        )
    if not _TOOL_NAME.fullmatch(text):
        raise ValueError(f"未知规则名 {text!r}，可选：Bash / Read / Edit，或 Tool(param:value)")
    name_key = text.lower()
    family = _NAME_TO_FAMILY.get(name_key, name_key)
    named = None if name_key in _NAME_TO_FAMILY else _NAME_TO_TOOL.get(name_key, name_key)
    return ParsedRule(family=family, pattern=None, raw=text, tool_name=named)


def domain_from_args(args: dict[str, Any]) -> str | None:
    """从 ``domain`` 或 ``url`` 取出主机名，供 ``WebFetch(domain:…)`` 匹配。"""
    raw = args.get("domain")
    if raw is not None and str(raw).strip():
        return _norm_host(str(raw))
    url = args.get("url")
    if url is None or not str(url).strip():
        return None
    text = str(url).strip()
    if "://" not in text:
        text = "https://" + text
    host = urlparse(text).hostname
    return _norm_host(host) if host else None


def match_param_value(pattern: str, value: Any) -> bool:
    """标量参数按原文比较；``*`` 通配。省略的参数不匹配。对象 / 数组不匹配。"""
    text = _scalar_text(value)
    if text is None:
        return False
    return match_bash_pattern(pattern, text)


def _norm_host(text: str) -> str:
    host = text.strip().lower().rstrip(".")
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    return host


def _scalar_text(value: Any) -> str | None:
    if value is None or isinstance(value, (dict, list)):
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def unwrap_segment_text(segment: str, *, peel_all_assignments: bool = True) -> str:
    """剥 env / timeout 等包装后，把 argv 拼回一行，供 bash 规则匹配。"""
    tokens = unwrap_argv(tokenize(segment)).tokens
    tokens = peel_leading_assignments(tokens, all_assignments=peel_all_assignments)
    return " ".join(tokens).strip()


def bash_subjects(command: str, *, peel_all_assignments: bool = True) -> list[str]:
    """复合命令每一段展开后的文本；空命令返回空列表。"""
    subjects: list[str] = []
    for segment in split_command_segments(command):
        text = unwrap_segment_text(segment, peel_all_assignments=peel_all_assignments)
        if text:
            subjects.append(text)
    if not subjects:
        stripped = command.strip()
        if stripped:
            subjects.append(stripped)
    return subjects


def match_bash_pattern(pattern: str, command: str) -> bool:
    """``*`` 匹配任意文本（含空格）。无 ``*`` 则整段精确匹配。

    末尾 `` *`` 且是唯一通配时，也匹配没有参数的裸命令：``pytest *`` 匹配 ``pytest``。
    """
    command = command.strip()
    pattern = pattern.strip()
    if not pattern or pattern == "*":
        return True
    if "*" not in pattern:
        return command == pattern
    only_trailing_star = pattern.endswith(" *") and pattern.count("*") == 1
    if _glob_to_re(pattern).fullmatch(command):
        return True
    if only_trailing_star:
        return command == pattern[:-2]
    return False


def match_path_pattern(
    pattern: str,
    relative: str,
    *,
    deep: bool,
    absolute: Path | None = None,
    workspace: Path | None = None,
    config_home: Path | None = None,
    source: str = "project",
) -> bool:
    """路径匹配。

    无前缀：deny/ask 的裸文件名与单段 ``dir/**`` 任意深度；allow 从工作区根算。
    ``/path``：相对设置源（用户设置 → ``~/.cyan``，其余 → 工作区），各规则类型同深度。
    ``~/path``：用户主目录；``//path``：文件系统绝对路径。
    """
    kind, rest = split_path_anchor(pattern)
    subject = _norm_path(relative)
    if kind == "relative":
        glob = _norm_path(rest)
        if not glob:
            return True
        if deep and "/" not in glob and "**" not in glob:
            return _path_glob_match(glob, subject) or _path_glob_match("**/" + glob, subject)
        if deep and _is_single_dir_starstar(glob):
            return _path_glob_match(glob, subject) or _path_glob_match("**/" + glob, subject)
        return _path_glob_match(glob, subject)

    file_abs = _file_absolute(relative, workspace=workspace, absolute=absolute)
    glob = _norm_path(rest) if kind != "abs" else _norm_abs_glob(rest)
    if kind == "abs":
        if file_abs is None:
            return False
        return _path_glob_match(glob, file_abs.resolve().as_posix())
    if kind == "home":
        if file_abs is None:
            return False
        try:
            under = file_abs.resolve().relative_to(Path.home().resolve()).as_posix()
        except (ValueError, OSError, RuntimeError):
            return False
        if not glob:
            return under in {".", ""}
        return _path_glob_match(glob, _norm_path(under))
    if not glob:
        return True
    anchor = config_home if source == "user" else workspace
    if file_abs is not None and anchor is not None:
        try:
            under = file_abs.resolve().relative_to(Path(anchor).resolve()).as_posix()
        except (ValueError, OSError, RuntimeError):
            return False
        return _path_glob_match(glob, _norm_path(under))
    return _path_glob_match(glob, subject)


def split_path_anchor(pattern: str) -> tuple[PathAnchor, str]:
    """拆出 ``//`` / ``~/`` / ``/`` 锚点，其余是工作区相对。"""
    text = pattern.replace("\\", "/").strip()
    while text.startswith("./"):
        text = text[2:]
    if text.startswith("//"):
        rest = text[2:].lstrip("/")
        return "abs", "/" + rest if rest else "/"
    if text == "~" or text.startswith("~/"):
        return "home", text[2:] if text.startswith("~/") else ""
    if text.startswith("/"):
        return "settings", text[1:]
    return "relative", text


def _file_absolute(
    relative: str, *, workspace: Path | None, absolute: Path | None
) -> Path | None:
    if absolute is not None:
        return absolute
    if workspace is None:
        return None
    try:
        candidate = Path(relative)
        if candidate.is_absolute():
            return candidate.resolve()
        return (Path(workspace) / relative).resolve()
    except (OSError, RuntimeError, ValueError):
        return None


def _norm_abs_glob(pattern: str) -> str:
    text = pattern.replace("\\", "/") or "/"
    if text != "/":
        text = text.rstrip("/") or "/"
    return text


def _is_single_dir_starstar(pattern: str) -> bool:
    """``src/**`` / ``.git/**``：单个目录段加 ``/**``，deny/ask 时匹配任意深度同名目录。"""
    if not pattern.endswith("/**"):
        return False
    head = pattern[:-3]
    return bool(head) and "/" not in head and "**" not in head


def _glob_to_re(pattern: str) -> re.Pattern[str]:
    """把 bash 规则里的 ``*`` 编成正则（``*`` = 任意文本）。"""
    parts: list[str] = []
    for char in pattern:
        parts.append(".*" if char == "*" else re.escape(char))
    return re.compile("^" + "".join(parts) + "$")


def _path_glob_match(pattern: str, path: str) -> bool:
    return bool(_gitignore_to_re(pattern).match(path))


def _gitignore_to_re(pattern: str) -> re.Pattern[str]:
    """简化 gitignore：``*`` 一段，``**`` 跨目录。``dir/**`` 匹配目录下的内容，不含目录本身。"""
    return re.compile("^" + _gitignore_body(pattern) + "$")


def _gitignore_body(pattern: str) -> str:
    parts: list[str] = []
    index = 0
    length = len(pattern)
    while index < length:
        if pattern.startswith("**/", index):
            parts.append("(?:.*/)?")
            index += 3
            continue
        if pattern.startswith("**", index):
            parts.append(".*")
            index += 2
            continue
        char = pattern[index]
        if char == "*":
            parts.append("[^/]*")
        elif char == "?":
            parts.append("[^/]")
        else:
            parts.append(re.escape(char))
        index += 1
    return "".join(parts)


def _norm_path(path: str) -> str:
    text = path.replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    return text.strip("/") if text != "." else text
