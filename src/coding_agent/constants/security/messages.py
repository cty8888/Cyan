"""Permission-related user-facing messages."""

PLAN_WRITE_MSG = (
    "当前处于 Plan 模式, 不允许修改文件. "
    "请向用户说明所需操作, 或建议切换到 Default / AcceptEdits 模式."
)

PLAN_EXEC_MSG = (
    "当前处于 Plan 模式, 仅允许只读 shell 命令 (如 git status, cat, pytest). "
    "请向用户说明所需操作, 或建议切换到 Default / AcceptEdits 模式."
)

USER_DENIED_MSG = (
    "用户拒绝了此操作. 请不要重试, 改用其他方案或询问用户的意见."
)

NO_PERMISSION_RULE_MSG = "No permission rule matched"
