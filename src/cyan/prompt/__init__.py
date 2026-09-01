"""Prompt Layer：identity、cyan.md、Skills 等指令在组窗时叠进 system 角色。"""

from .files import INSTRUCTION_FILENAME, load_instruction_layers, project_instruction_path
from .skills import (
    SkillMeta,
    discover_skills,
    load_skill_layers,
    set_skill_enabled,
    skill_settings_path,
    skills_layer_enabled,
)
from .stack import PromptStack
from .types import PromptLayer, PromptLayerKind

__all__ = [
    "INSTRUCTION_FILENAME",
    "PromptLayer",
    "PromptLayerKind",
    "PromptStack",
    "SkillMeta",
    "discover_skills",
    "load_instruction_layers",
    "load_skill_layers",
    "project_instruction_path",
    "set_skill_enabled",
    "skill_settings_path",
    "skills_layer_enabled",
]
