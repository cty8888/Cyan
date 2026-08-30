"""Prompt Layer 的数据契约。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path


class PromptLayerKind(Enum):
    """一层 system 提示的来源。以后可加 NESTED_INSTRUCTIONS / AUTO_MEMORY。"""

    IDENTITY = "identity"
    USER_INSTRUCTIONS = "user_instructions"
    PROJECT_INSTRUCTIONS = "project_instructions"
    AUTO_MEMORY = "auto_memory"


@dataclass
class PromptLayer:
    """一等公民的提示层：有 kind、来源与正文，不是被拼进 identity 的字符串。"""

    kind: PromptLayerKind
    title: str
    text: str
    source: Path | None = None
    truncated: bool = False
