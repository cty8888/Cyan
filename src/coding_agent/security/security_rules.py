"""安全规则：Blocked / Restricted / Sensitive 三级分类。

路径规则适用于 write_file、edit_file 及 bash 命令 token 扫描；
命令规则适用于 bash 的 command 文本。write 与 exec 共用同一套级别。
"""

from __future__ import annotations

import re
from pathlib import Path

# ------------------------------------------------------------------ 命令：Blocked
BLOCKED_COMMANDS: list[tuple[re.Pattern[str], str]] = [
    (
        re.compile(r"\brm\b[^|;&]*\s-[a-zA-Z]*[rf][a-zA-Z]*[^|;&]*\s+(/|~|/\*|\$HOME)(\s|$|/?\*?$)"),
        "递归删除根目录或用户主目录",
    ),
    (re.compile(r"\bmkfs(\.\w+)?\b"), "格式化文件系统"),
    (re.compile(r"\bdd\b[^|;&]*\bof=/dev/"), "向块设备直接写入"),
    (re.compile(r">\s*/dev/(sd|nvme|hd|disk)"), "覆写块设备"),
    (re.compile(r"\b(shutdown|reboot|poweroff|halt)\b"), "关机或重启主机"),
    (re.compile(r":\s*\(\s*\)\s*\{.*\|.*&.*\}\s*;?\s*:"), "fork 炸弹"),
    (re.compile(r"\b(curl|wget)\b[^|]*\|\s*(sudo\s+)?(ba|z|k)?sh\b"), "下载并直接执行远程脚本"),
    (re.compile(r"\bsudo\b"), "提权执行"),
    (re.compile(r"\bchmod\b\s+(-R\s+)?777\s+/(\s|$)"), "对根目录放开全部权限"),
    (re.compile(r"\bchown\b\s+-R\s+[^\s]+\s+/(\s|$)"), "递归修改根目录属主"),
]

# ------------------------------------------------------------------ 命令：Restricted
RESTRICTED_COMMANDS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\bgit\s+push\b[^|;&]*(-f|--force)\b"), "强制推送到远程仓库"),
    (re.compile(r"\bgit\s+reset\b[^|;&]*--hard\b"), "硬重置 Git 历史"),
    (re.compile(r"\bgit\s+clean\b[^|;&]*(-f|-d|-x)"), "破坏性清理未跟踪文件"),
    (re.compile(r"\bDROP\s+(TABLE|DATABASE)\b", re.I), "删除数据库表或库"),
    (re.compile(r"\bTRUNCATE\b", re.I), "清空数据库表"),
    (re.compile(r"\brm\b[^|;&]*\s-[a-zA-Z]*[rf][a-zA-Z]*"), "递归强制删除"),
    (re.compile(r"\bkill\b[^|;&]*(-9|\s+9\b)"), "强制终止进程"),
    (re.compile(r"\bkillall\b"), "批量终止进程"),
    (re.compile(r"\bchmod\b[^|;&]*(-R\s+)?777\b"), "放开全部权限"),
    (re.compile(r"\bchown\b[^|;&]*-R\b"), "递归修改属主"),
]

# ------------------------------------------------------------------ 命令：Sensitive
SENSITIVE_COMMANDS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\bgit\s+push\b"), "推送到远程仓库"),
    (re.compile(r"\bgit\s+commit\b"), "提交 Git 变更"),
    (re.compile(r"\bpip3?\s+install\b"), "安装 Python 包"),
    (re.compile(r"\bpip3?\s+uninstall\b"), "卸载 Python 包"),
    (re.compile(r"\bnpm\s+install\b"), "安装 npm 包"),
    (re.compile(r"\bnpm\s+uninstall\b"), "卸载 npm 包"),
    (re.compile(r"\bcargo\s+add\b"), "添加 Rust 依赖"),
    (re.compile(r"\bcurl\b"), "网络请求"),
    (re.compile(r"\bwget\b"), "网络下载"),
]

# ------------------------------------------------------------------ 路径
SENSITIVE_NAMES = {".env", "id_rsa", "id_ed25519", "credentials", ".npmrc", ".netrc"}
SENSITIVE_SUFFIXES = {".pem", ".key", ".p12"}
SENSITIVE_DIRS = {".ssh", ".aws", ".config"}
RESTRICTED_DIR_PARTS = {".git"}

COMMAND_TOKEN_SEPARATOR = re.compile(r"""[\s;|&<>()'"`]+""")


def normalize_command(command: str) -> str:
    return " ".join(str(command).split())


def match_command_rules(
    command: str, rules: list[tuple[re.Pattern[str], str]]
) -> str | None:
    normalized = normalize_command(command)
    for pattern, reason in rules:
        if pattern.search(normalized):
            return reason
    return None


def is_sensitive_path(path: Path) -> bool:
    name = path.name
    if name in SENSITIVE_NAMES or name.startswith(".env"):
        return True
    if path.suffix in SENSITIVE_SUFFIXES:
        return True
    return bool(set(path.parts) & SENSITIVE_DIRS)


def is_restricted_path(path: Path) -> bool:
    return bool(set(path.parts) & RESTRICTED_DIR_PARTS)


def iter_command_tokens(command: str) -> list[str]:
    tokens: list[str] = []
    for token in COMMAND_TOKEN_SEPARATOR.split(command):
        token = token.strip()
        if token and not token.startswith("-"):
            tokens.append(token)
    return tokens
