"""Permission mode display labels."""

from ...security.modes import PermissionMode

MODE_LABELS = {
    PermissionMode.PLAN: "Plan (只读规划)",
    PermissionMode.DEFAULT: "Default (默认)",
    PermissionMode.ACCEPT_EDITS: "AcceptEdits (自动批准编辑)",
    PermissionMode.BYPASS: "Bypass (跳过权限)",
}
