from __future__ import annotations

from enum import Enum


class PermissionMode(Enum):
    PLAN = "plan"
    DEFAULT = "default"
    ACCEPT_EDITS = "accept_edits"
    BYPASS = "bypass"
