"""从磁盘读出用户级 / 项目级 cyan.md，变成 PromptLayer。"""

from __future__ import annotations

from pathlib import Path

from ..settings.tools import DEFAULT_TOOL_RESULT_CHARS
from .types import PromptLayer, PromptLayerKind

INSTRUCTION_FILENAME = "cyan.md"
PROJECT_INSTRUCTION_DIR = ".cyan"
_TRUNCATION_MARKER = "...[truncated]"

_USER_TITLE = "用户指令"
_PROJECT_TITLE = "项目指令"


def project_instruction_path(workspace: Path) -> Path:
    """项目指令正式位置是 ``.cyan/cyan.md``；没有时退回仓库根 ``cyan.md``。"""
    nested = Path(workspace) / PROJECT_INSTRUCTION_DIR / INSTRUCTION_FILENAME
    if nested.exists():
        return nested
    return Path(workspace) / INSTRUCTION_FILENAME


def load_instruction_layers(
    workspace: Path,
    *,
    home: Path | None = None,
    max_chars: int = DEFAULT_TOOL_RESULT_CHARS,
) -> list[PromptLayer]:
    """按从宽到窄顺序加载文件层；缺文件、空文件、逃出根目录的一律跳过。

    ``home is None`` 时不读用户级，避免测试误加载开发者本机 ``~/.cyan/cyan.md``。
    """
    layers: list[PromptLayer] = []
    if home is not None:
        user = load_file_layer(
            Path(home) / INSTRUCTION_FILENAME,
            root=Path(home),
            kind=PromptLayerKind.USER_INSTRUCTIONS,
            title=_USER_TITLE,
            max_chars=max_chars,
        )
        if user is not None:
            layers.append(user)
    project = load_file_layer(
        project_instruction_path(workspace),
        root=Path(workspace),
        kind=PromptLayerKind.PROJECT_INSTRUCTIONS,
        title=_PROJECT_TITLE,
        max_chars=max_chars,
    )
    if project is not None:
        layers.append(project)
    return layers


def load_file_layer(
    path: Path,
    *,
    root: Path,
    kind: PromptLayerKind,
    title: str,
    max_chars: int,
) -> PromptLayer | None:
    if not path.exists():
        return None
    try:
        resolved = path.resolve()
        root_resolved = Path(root).resolve()
    except OSError:
        return None
    if not resolved.is_file():
        return None
    if not is_inside(root_resolved, resolved):
        return None
    try:
        text = resolved.read_text(encoding="utf-8")
    except OSError:
        return None
    text = text.strip()
    if not text:
        return None
    truncated = False
    if max_chars > 0 and len(text) > max_chars:
        text, truncated = truncate_text(text, max_chars)
    return PromptLayer(kind=kind, title=title, text=text, source=resolved, truncated=truncated)


def is_inside(root: Path, resolved: Path) -> bool:
    """``resolved`` 必须是 ``root`` 本身或根下的文件。符号链接先 resolve 再判定。"""
    if resolved == root:
        return True
    return root in resolved.parents


def truncate_text(text: str, limit: int) -> tuple[str, bool]:
    keep = max(0, limit - len(_TRUNCATION_MARKER))
    return text[:keep] + _TRUNCATION_MARKER, True
