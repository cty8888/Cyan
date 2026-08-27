"""安全策略：路径沙箱、命令黑名单、风险分级。

三道防线：

1. **沙箱**：所有文件路径 ``resolve()`` 后必须落在工作目录内，符号链接与 ``..`` 逃逸都会被拦下。
2. **黑名单**：致命命令直接拒绝执行，``--yolo`` 和「本会话始终允许」都无法绕过。
3. **分级审批**：只读操作自动放行，写入与执行需要用户确认；敏感文件强制逐次确认。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..errors import BlockedCommandError, PathOutsideWorkspaceError
from .approval import ApprovalRequest

if TYPE_CHECKING:  # 仅用于类型标注，避免与 tools 包循环导入
    from ..tools.base import Tool

# (正则, 说明)。命中即拒绝，任何模式下都不放行。
_BLOCKED_COMMANDS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\brm\b[^|;&]*\s-[a-zA-Z]*[rf][a-zA-Z]*[^|;&]*\s+(/|~|/\*|\$HOME)(\s|$|/?\*?$)"), "递归删除根目录或用户主目录"),
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

# 命中即使在 --yolo 下也要逐次确认
_SENSITIVE_NAMES = {".env", "id_rsa", "id_ed25519", "credentials", ".npmrc", ".netrc"}
_SENSITIVE_SUFFIXES = {".pem", ".key", ".p12"}
_SENSITIVE_DIRS = {".git", ".ssh", ".aws", ".config"}

# 把 shell 命令拆成疑似路径的 token，用于检查命令是否触碰敏感文件
_COMMAND_TOKEN_SEPARATOR = re.compile(r"""[\s;|&<>()'"`]+""")


class SecurityPolicy:
    def __init__(self, workspace: Path, yolo: bool = False):
        self.workspace = Path(workspace).resolve()
        self.yolo = yolo

    # ---------------------------------------------------------------- 路径
    def resolve_path(self, raw: str, *, must_exist: bool = False) -> Path:
        """把工具参数中的路径解析为绝对路径，并校验未逃出沙箱。"""
        if raw is None or str(raw).strip() == "":
            raise PathOutsideWorkspaceError("路径不能为空")

        candidate = Path(str(raw)).expanduser()
        if not candidate.is_absolute():
            candidate = self.workspace / candidate

        # resolve 会展开符号链接，因此软链接逃逸同样能被下面的归属检查挡住
        resolved = candidate.resolve()
        if resolved != self.workspace and self.workspace not in resolved.parents:
            raise PathOutsideWorkspaceError(
                f"路径 {raw} 解析为 {resolved}，位于工作目录 {self.workspace} 之外，已拒绝访问。"
                "只能操作工作目录内的文件。"
            )
        if must_exist and not resolved.exists():
            raise FileNotFoundError(f"路径不存在：{self.display(resolved)}")
        return resolved

    def display(self, path: Path) -> str:
        """转成相对工作目录的短路径，用于展示和回喂模型。"""
        try:
            return str(path.relative_to(self.workspace)) or "."
        except ValueError:
            return str(path)

    def is_sensitive(self, path: Path) -> bool:
        name = path.name
        if name in _SENSITIVE_NAMES or name.startswith(".env"):
            return True
        if path.suffix in _SENSITIVE_SUFFIXES:
            return True
        parts = set(path.parts)
        return bool(parts & _SENSITIVE_DIRS)

    # ---------------------------------------------------------------- 命令
    def check_command(self, command: str) -> None:
        """命中黑名单则抛出 ``BlockedCommandError``。"""
        normalized = " ".join(str(command).split())
        for pattern, reason in _BLOCKED_COMMANDS:
            if pattern.search(normalized):
                raise BlockedCommandError(
                    f"命令被安全策略拦截（{reason}）：{normalized}。"
                    "该限制无法通过授权绕过，请改用更安全的做法。"
                )

    # ------------------------------------------------------------ 风险分级
    def build_approval(self, tool: Tool, args: dict[str, Any]) -> ApprovalRequest | None:
        """只读工具返回 None；其余生成待确认请求。"""
        from ..tools.base import RiskLevel

        if tool.risk is RiskLevel.READ:
            return None

        summary, detail, detail_format = tool.describe(args, self)
        force, reason = self._force_confirm(tool, args)
        return ApprovalRequest(
            tool_name=tool.name,
            risk=tool.risk.value,
            summary=summary,
            detail=detail,
            detail_format=detail_format,
            force=force,
            reason=reason,
        )

    def _force_confirm(self, tool: Tool, args: dict[str, Any]) -> tuple[bool, str | None]:
        raw_path = args.get("path") or args.get("file")
        if raw_path:
            try:
                resolved = self.resolve_path(str(raw_path))
            except PathOutsideWorkspaceError:
                return True, "路径可疑"
            if self.is_sensitive(resolved):
                return True, f"{self.display(resolved)} 属于敏感文件，每次写入都需要确认"

        # 光看 path 参数会漏掉 `echo x > .env` 这类绕过，命令文本也要扫一遍
        command = args.get("command")
        if command:
            hit = self._sensitive_token(str(command))
            if hit:
                return True, f"命令中出现敏感文件 {hit}，每次执行都需要确认"
        return False, None

    def _sensitive_token(self, command: str) -> str | None:
        """返回命令里第一个指向敏感文件的 token，没有则返回 None。"""
        for token in _COMMAND_TOKEN_SEPARATOR.split(command):
            token = token.strip()
            if not token or token.startswith("-"):
                continue
            if self.is_sensitive(Path(token)):
                return token
        return None
