"""工作区路径沙箱：解析、展示，以及给规则/白名单用的写目标路径。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..errors import PathOutsideWorkspaceError


def resolve_path(
    workspace: Path, raw: str, *, must_exist: bool = False, base: Path | None = None
) -> Path:
    """把工具参数中的路径解析为绝对路径，并校验未逃出沙箱。

    ``base`` 是相对路径的起点（bash 的会话 cwd）；默认工作目录根。
    无论从哪起算，解析结果都必须落在工作区内。
    """
    root = Path(workspace).resolve()
    if raw is None or str(raw).strip() == "":
        raise PathOutsideWorkspaceError("路径不能为空")

    candidate = Path(str(raw)).expanduser()
    if not candidate.is_absolute():
        origin = Path(base).resolve() if base is not None else root
        candidate = origin / candidate

    resolved = candidate.resolve()
    if resolved != root and root not in resolved.parents:
        raise PathOutsideWorkspaceError(
            f"路径 {raw} 解析为 {resolved}，位于工作目录 {root} 之外，已拒绝访问。"
            "只能操作工作目录内的文件。"
        )
    if must_exist and not resolved.exists():
        raise FileNotFoundError(f"路径不存在：{display(root, resolved)}")
    return resolved


def display(workspace: Path, path: Path) -> str:
    """把绝对路径收成相对工作区的展示路径；不在工作区内则退回原路径。"""
    root = Path(workspace).resolve()
    try:
        return str(path.resolve().relative_to(root)) or "."
    except ValueError:
        return str(path)


def write_target_display(workspace: Path, args: dict[str, Any]) -> str | None:
    """把 write 类工具的 path 参数收成相对工作目录的展示路径，供规则与白名单匹配。

    解析失败（空路径、逃出沙箱等）时退回原始字符串，只影响匹配准确性，
    不在这里抛异常——真正的路径合法性校验交给工具自己的 ``resolve_path``。
    """
    raw = args.get("path")
    if raw is None:
        return None
    raw = str(raw)
    try:
        resolved = resolve_path(workspace, raw)
    except Exception:
        return raw
    return display(workspace, resolved)
