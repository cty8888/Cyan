"""PromptStack：按层顺序装配发给模型的 system 正文。"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from ..memory.settings import auto_memory_enabled
from ..memory.store import load_memory_index_layer
from ..settings.tools import DEFAULT_TOOL_RESULT_CHARS
from .files import load_instruction_layers
from .types import PromptLayer, PromptLayerKind


@dataclass
class PromptStack:
    """持有 identity、cyan.md 与 MEMORY.md 索引。组窗时渲染；不进 Session。"""

    workspace: Path
    home: Path | None = None
    max_chars: int = DEFAULT_TOOL_RESULT_CHARS
    auto_memory: bool = False
    identity: PromptLayer | None = None
    extra: list[PromptLayer] = field(default_factory=list)

    def set_identity(self, text: str) -> None:
        """用当前会话的 identity 系统提示作为第一层。"""
        self.identity = PromptLayer(
            kind=PromptLayerKind.IDENTITY,
            title="身份",
            text=text or "",
            source=None,
        )

    def refresh_files(self) -> None:
        """从磁盘重读 cyan.md 与 MEMORY.md 索引。"""
        self.extra = load_instruction_layers(
            self.workspace, home=self.home, max_chars=self.max_chars
        )
        if self.auto_memory and auto_memory_enabled():
            memory = load_memory_index_layer(self.workspace)
            if memory is not None:
                self.extra.append(memory)

    def layers(self) -> list[PromptLayer]:
        """当前要发给模型的层：identity（若有正文）在前，文件层随后。"""
        result: list[PromptLayer] = []
        if self.identity is not None and self.identity.text:
            result.append(self.identity)
        result.extend(self.extra)
        return result

    def render_system(self, identity_text: str) -> str:
        """叠层后的 system 正文。无文件层时原样返回 identity。"""
        self.set_identity(identity_text)
        self.refresh_files()
        if not self.extra:
            return identity_text
        parts: list[str] = []
        if identity_text:
            parts.append(identity_text)
        for layer in self.extra:
            parts.append(_render_file_layer(layer))
        return "\n\n".join(parts)


def _render_file_layer(layer: PromptLayer) -> str:
    source = str(layer.source) if layer.source is not None else ""
    header = f"# 指令层 · {layer.title}"
    if source:
        header += f"\n来源：`{source}`"
    return f"{header}\n\n{layer.text}"
