"""是否启用项目级 Auto Memory。"""

from __future__ import annotations

import os

from .types import ENV_DISABLE


def auto_memory_enabled() -> bool:
    raw = os.environ.get(ENV_DISABLE, "").strip().lower()
    return raw not in {"1", "true", "yes", "on"}
