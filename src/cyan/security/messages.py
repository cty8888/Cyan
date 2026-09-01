"""权限相关的用户可见文案。"""

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

NO_PERMISSION_RULE_MSG = "没有匹配到适用的权限规则。"

ENV_DUMP_MSG = "会打印进程环境变量，可能包含 API Key，每次都需要确认。"

UNBOUNDED_READ_MSG = (
    "这条命令含通配符或递归搜索，可能读到 .env / 密钥，每次都需要确认。"
)

UNRESOLVED_CHDIR_MSG = (
    "无法确认 cd / pushd 的目标目录（例如变量或 popd），"
    "拒绝执行以免越出工作区。"
)
