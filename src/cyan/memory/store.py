"""项目级自动记忆：只读写 ``{workspace}/.cyan/memory/`` 下的固定文件名。"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from ..errors import PathOutsideWorkspaceError, ToolError
from ..prompt.files import is_inside, load_file_layer, truncate_text
from ..prompt.types import PromptLayer, PromptLayerKind
from .types import (
    ALLOWED_FILENAMES,
    INDEX_FILENAME,
    KIND_FILENAMES,
    MAX_INDEX_CHARS,
    MAX_INDEX_LINES,
    MemoryEntry,
    MemoryKind,
)

_INDEX_HEADER = "# Auto Memory\n"
_MEMORY_TITLE = "自动记忆"


def memory_dir(workspace: Path) -> Path:
    return Path(workspace).resolve() / ".cyan" / "memory"


def resolve_memory_file(workspace: Path, name: str) -> Path:
    """把合法文件名解析到 memory 目录内；符号链接逃出工作区则拒绝。"""
    if name not in ALLOWED_FILENAMES:
        allowed = ", ".join(sorted(ALLOWED_FILENAMES))
        raise ToolError(f"不能访问 {name}。自动记忆只允许：{allowed}")
    root = Path(workspace).resolve()
    directory = memory_dir(workspace)
    candidate = directory / name
    try:
        resolved = candidate.resolve()
    except OSError as exc:
        raise ToolError(f"无法解析记忆文件 {name}：{exc}") from exc
    if not is_inside(root, resolved):
        raise PathOutsideWorkspaceError(f"记忆文件 {name} 解析到工作区之外，已拒绝。")
    if directory.exists():
        mem_root = directory.resolve()
        if not is_inside(root, mem_root):
            raise PathOutsideWorkspaceError("记忆目录不在工作区内，已拒绝。")
        if resolved != mem_root and not is_inside(mem_root, resolved):
            raise PathOutsideWorkspaceError(
                f"记忆文件 {name} 不在 .cyan/memory/ 内，已拒绝。"
            )
    return resolved


def list_memory_files(workspace: Path) -> list[tuple[str, int]]:
    """返回 (文件名, 字节数)，按文件名排序。目录不存在则为空。"""
    directory = memory_dir(workspace)
    if not directory.is_dir():
        return []
    items: list[tuple[str, int]] = []
    for name in sorted(ALLOWED_FILENAMES):
        path = directory / name
        if path.is_file():
            items.append((name, path.stat().st_size))
    return items


def read_memory_file(workspace: Path, name: str) -> str:
    path = resolve_memory_file(workspace, name)
    if not path.is_file():
        raise ToolError(f"{name} 还不存在。")
    return path.read_text(encoding="utf-8")


def write_entry(workspace: Path, entry: MemoryEntry, *, mode: str = "append") -> bool:
    """写入类型文件并更新索引。与 cyan.md / 已有索引重复则返回 False。"""
    summary = " ".join(entry.summary.split())
    if not summary:
        return False
    if _is_duplicate(workspace, summary):
        return False
    kind_name = KIND_FILENAMES[entry.kind]
    body = (entry.detail or summary).strip()
    if mode == "replace":
        _write_kind_file(workspace, kind_name, _kind_section(body))
        _rewrite_index_kind(workspace, entry.kind, summary)
    else:
        _append_kind_file(workspace, kind_name, _kind_section(body))
        _append_index_line(workspace, entry.kind, summary)
    return True


def load_memory_index_layer(workspace: Path) -> PromptLayer | None:
    """组窗只加载 MEMORY.md 索引，类型文件不进 system。"""
    path = memory_dir(workspace) / INDEX_FILENAME
    layer = load_file_layer(
        path,
        root=Path(workspace),
        kind=PromptLayerKind.AUTO_MEMORY,
        title=_MEMORY_TITLE,
        max_chars=0,
    )
    if layer is None:
        return None
    text, truncated = _clip_index(layer.text)
    return PromptLayer(
        kind=layer.kind,
        title=layer.title,
        text=text,
        source=layer.source,
        truncated=truncated or layer.truncated,
    )


def _is_duplicate(workspace: Path, summary: str) -> bool:
    needle = summary.casefold()
    index_path = memory_dir(workspace) / INDEX_FILENAME
    if index_path.is_file():
        existing = index_path.read_text(encoding="utf-8")
        if needle in existing.casefold():
            return True
    from ..prompt.files import project_instruction_path

    cyan_path = project_instruction_path(workspace)
    if cyan_path.is_file():
        try:
            if needle in cyan_path.read_text(encoding="utf-8").casefold():
                return True
        except OSError:
            pass
    return False


def _kind_section(body: str) -> str:
    return f"## {date.today().isoformat()}\n\n{body.strip()}\n"


def _append_kind_file(workspace: Path, name: str, section: str) -> None:
    path = _ensure_file(workspace, name)
    current = path.read_text(encoding="utf-8") if path.is_file() else ""
    if current and not current.endswith("\n"):
        current += "\n"
    path.write_text(current + ("\n" if current.strip() else "") + section, encoding="utf-8")


def _write_kind_file(workspace: Path, name: str, section: str) -> None:
    path = _ensure_file(workspace, name)
    header = f"# {name.removesuffix('.md')}\n\n"
    path.write_text(header + section, encoding="utf-8")


def _append_index_line(workspace: Path, kind: MemoryKind, summary: str) -> None:
    path = _ensure_file(workspace, INDEX_FILENAME)
    text = path.read_text(encoding="utf-8") if path.is_file() else ""
    if not text.strip():
        text = _INDEX_HEADER
    if not text.endswith("\n"):
        text += "\n"
    text += f"- [{kind.value}] {summary}\n"
    clipped, _ = _clip_index(text)
    if not clipped.endswith("\n"):
        clipped += "\n"
    path.write_text(clipped, encoding="utf-8")


def _rewrite_index_kind(workspace: Path, kind: MemoryKind, summary: str) -> None:
    path = _ensure_file(workspace, INDEX_FILENAME)
    text = path.read_text(encoding="utf-8") if path.is_file() else _INDEX_HEADER
    prefix = f"- [{kind.value}] "
    kept = [line for line in text.splitlines() if not line.startswith(prefix)]
    if not kept or kept[0].strip() != "# Auto Memory":
        kept = ["# Auto Memory", *kept]
    kept.append(f"- [{kind.value}] {summary}")
    path.write_text("\n".join(kept) + "\n", encoding="utf-8")
    clipped, _ = _clip_index(path.read_text(encoding="utf-8"))
    path.write_text(clipped if clipped.endswith("\n") else clipped + "\n", encoding="utf-8")


def _ensure_file(workspace: Path, name: str) -> Path:
    path = resolve_memory_file(workspace, name)
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _clip_index(text: str) -> tuple[str, bool]:
    truncated = False
    lines = text.splitlines()
    if len(lines) > MAX_INDEX_LINES:
        lines = lines[:MAX_INDEX_LINES]
        truncated = True
    clipped = "\n".join(lines)
    if len(clipped) > MAX_INDEX_CHARS:
        clipped, _ = truncate_text(clipped, MAX_INDEX_CHARS)
        truncated = True
    return clipped, truncated
