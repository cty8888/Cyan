"""安全规则分级：Blocked / Restricted / Sensitive 规则表。

三级规则统一支持 write（路径）与 exec（命令）两种输入：

- **Blocked（黑名单）**：永远 ``DENY``，任何权限模式（包括 Bypass）都不能绕过，
  也不出现在审批 UI 里——因为已经不是"要不要批准"的问题。
- **Restricted（强硬限制）**：同样直接 ``DENY``、不出审批 UI，但语义上比 Blocked
  温和一点：不是"永远致命"，而是"这类操作不应该由 Agent 自动执行"
  （如强推、硬重置、直接改仓库元数据）。
- **Sensitive（敏感）**：``NEED_APPROVAL`` 且 ``force=True``——不受"本会话始终允许"
  或 AcceptEdits / Bypass 模式影响，每次都要过一遍人。

命中优先级：Blocked > Restricted > Sensitive > Normal。Normal 级别由
``PermissionManager`` 按 ``PermissionMode`` 现有逻辑处理，不在这个文件里。

这里的规则表是启发式的、可以持续补充，不追求覆盖所有可能的危险命令。
"""

from __future__ import annotations

import re

from ..errors import SecurityError

# --------------------------------------------------------------------------- 命令规则


def _word(pattern: str) -> re.Pattern[str]:
    """编译一条命令规则。单独抽出是为了表格里写起来短一点。"""
    return re.compile(pattern)


_BLOCKED_COMMAND_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    (_word(r"(?<![\w.-])sudo(?![\w.-])"), "sudo 提权命令永远拒绝，不支持任何形式的授权。"),
    (
        _word(r"\brm\s+(-\w*[rf]\w*[rf]?\w*|--recursive|--force)\b.*(\s|=)(/\s*$|/\*|~\s*$|~/|\$HOME\b)"),
        "检测到可能删除根目录 / 家目录的 rm -rf，永远拒绝。",
    ),
    (
        _word(r"\brm\s+(-\w*[rf]\w*[rf]?\w*|--recursive|--force)\b\s+(\.\s*$|\*(?!\S))"),
        "检测到删除当前目录或通配全部文件的 rm -rf，永远拒绝。",
    ),
    (_word(r"\bmkfs(\.\w+)?\b"), "mkfs 会格式化磁盘，永远拒绝。"),
    (_word(r"\bdd\b[^\n]*\bof=/dev/"), "dd 写入裸设备可能直接破坏磁盘，永远拒绝。"),
    (_word(r":\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:"), "检测到 fork bomb，永远拒绝。"),
    (
        _word(r"\b(curl|wget)\b[^\n|]*\|\s*(sudo\s+)?(sh|bash|zsh|python\d?)\b"),
        "从远端下载脚本后直接执行（curl/wget | sh），永远拒绝。",
    ),
    (
        _word(r"\b(curl|wget)\b[^\n]*(&&|;)\s*(sudo\s+)?(sh|bash|zsh|python\d?)\b"),
        "从远端下载脚本后用 && 直接执行，永远拒绝。",
    ),
    (_word(r"\bchmod\b\s+(-R|--recursive)\s+777\s+/\s*$"), "递归给根目录加全部可写权限，永远拒绝。"),
    (_word(r"\b(shutdown|reboot|halt|poweroff)\b"), "关机 / 重启命令永远拒绝。"),
    (_word(r">\s*/dev/(sd|nvme|hd)[a-z0-9]*\b"), "直接写入磁盘设备节点，永远拒绝。"),
)

_RESTRICTED_COMMAND_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        _word(r"\bgit\s+push\b[^\n]*(--force\b|-f\b)"),
        "git push --force 会覆盖远端历史，Agent 不能自动执行，请手动处理。",
    ),
    (
        _word(r"\bgit\s+reset\b[^\n]*--hard\b"),
        "git reset --hard 会丢弃未提交的修改，Agent 不能自动执行。",
    ),
    (
        _word(r"\bgit\s+clean\b[^\n]*-[a-zA-Z]*f[a-zA-Z]*d|\bgit\s+clean\b[^\n]*-[a-zA-Z]*d[a-zA-Z]*f"),
        "git clean -fd 会不可恢复地删除未跟踪文件，Agent 不能自动执行。",
    ),
    (
        _word(r"\bgit\s+checkout\b[^\n]*--\s+\.|\bgit\s+checkout\b[^\n]*\s+\.\s*$"),
        "git checkout . 会丢弃工作区未提交的修改，Agent 不能自动执行。",
    ),
)

_SENSITIVE_COMMAND_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    (_word(r"\b(pip|pip3)\s+(install|uninstall)\b"), "会安装 / 卸载 Python 依赖，需要确认。"),
    (_word(r"\b(npm|yarn|pnpm)\s+(install|add|remove|uninstall)\b"), "会修改项目依赖，需要确认。"),
    (_word(r"\bgit\s+push\b"), "会推送到远端仓库，需要确认。"),
)


def blocked_command(command: str) -> str | None:
    """命中黑名单返回拒绝理由；未命中返回 ``None``。"""
    return _first_match(_BLOCKED_COMMAND_RULES, command)


def restricted_command(command: str) -> str | None:
    """命中强硬限制返回拒绝理由；未命中返回 ``None``。"""
    return _first_match(_RESTRICTED_COMMAND_RULES, command)


def sensitive_command(command: str) -> str | None:
    """命中敏感规则返回需要强制确认的理由；未命中返回 ``None``。"""
    return _first_match(_SENSITIVE_COMMAND_RULES, command)


def _first_match(rules: tuple[tuple[re.Pattern[str], str], ...], command: str) -> str | None:
    """按表顺序返回第一条命中规则的拒绝/确认理由。"""
    for pattern, reason in rules:
        if pattern.search(command):
            return reason
    return None


# --------------------------------------------------------------------------- 路径规则

# 仓库元数据目录：写入即视为 Restricted，不管具体改的是哪个文件。
_RESTRICTED_PATH_DIRS: tuple[str, ...] = (".git/",)

# 敏感文件：按文件名整体匹配（不区分大小写），或者按后缀匹配。
_SENSITIVE_PATH_NAMES: frozenset[str] = frozenset(
    {
        ".npmrc",
        ".netrc",
        "credentials.json",
        "id_rsa",
        "id_ed25519",
        "id_ecdsa",
    }
)
_SENSITIVE_PATH_PREFIXES: tuple[str, ...] = (".env",)  # .env / .env.local / .env.production ...
_SENSITIVE_PATH_SUFFIXES: tuple[str, ...] = (".pem", ".key", ".p12", ".pfx")
_SENSITIVE_PATH_DIRS: tuple[str, ...] = (".ssh/",)


def reject_restricted_write(relative_path: str) -> None:
    """执行层二次拦截：命中 Restricted 路径时抛 ``SecurityError``。"""
    reason = restricted_path(relative_path)
    if reason:
        raise SecurityError(reason)


def restricted_path(relative_path: str) -> str | None:
    """路径命中仓库元数据等强硬限制目录时返回理由；未命中返回 ``None``。"""
    normalized = _normalize(relative_path)
    for marker in _RESTRICTED_PATH_DIRS:
        if _contains_dir_segment(normalized, marker):
            return f"{marker} 是仓库元数据目录，Agent 不能直接写入，请手动处理。"
    return None


def sensitive_path(relative_path: str) -> str | None:
    """路径命中敏感文件规则时返回需要强制确认的理由；未命中返回 ``None``。"""
    normalized = _normalize(relative_path)
    name = normalized.rsplit("/", 1)[-1].lower()

    if name in _SENSITIVE_PATH_NAMES:
        return f"{relative_path} 可能包含密钥 / 凭据，每次写入都需要确认。"
    if any(name.startswith(prefix) for prefix in _SENSITIVE_PATH_PREFIXES):
        return f"{relative_path} 是环境变量文件，可能包含密钥，每次写入都需要确认。"
    if any(name.endswith(suffix) for suffix in _SENSITIVE_PATH_SUFFIXES):
        return f"{relative_path} 看起来是私钥 / 证书文件，每次写入都需要确认。"
    for marker in _SENSITIVE_PATH_DIRS:
        if _contains_dir_segment(normalized, marker):
            return f"{relative_path} 位于 {marker} 目录下，每次写入都需要确认。"
    return None


def _normalize(relative_path: str) -> str:
    """统一分隔符并去掉前导 ``./``，供路径规则做子串/分段匹配。"""
    text = relative_path.replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    return text


def _contains_dir_segment(normalized_path: str, dir_marker: str) -> bool:
    """判断路径的某一级目录是否等于 ``dir_marker``（如 ``.git/``），不管它出现在第几层。"""
    dirname = dir_marker.rstrip("/")
    segments = normalized_path.split("/")[:-1]  # 去掉最后一段（文件名本身）
    return dirname in segments
