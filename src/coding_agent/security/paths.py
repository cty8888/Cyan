"""工作区路径沙箱：解析与展示。"""

from __future__ import annotations

from pathlib import Path

from ..errors import PathOutsideWorkspaceError


def resolve_path(workspace: Path, raw: str, *, must_exist: bool = False) -> Path:
    """把工具参数中的路径解析为绝对路径，并校验未逃出沙箱。"""
    root = Path(workspace).resolve()
    if raw is None or str(raw).strip() == "":
        raise PathOutsideWorkspaceError("路径不能为空")

    candidate = Path(str(raw)).expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate

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
    root = Path(workspace).resolve()
    try:
        return str(path.resolve().relative_to(root)) or "."
    except ValueError:
        return str(path)
