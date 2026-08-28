"""bash tool constants."""

BASH_NAME = "bash"

BASH_DESCRIPTION = (
    "在项目工作目录下执行 shell 命令 (测试, 构建, git, 脚本等). "
    "每条命令都在独立的新进程里运行; 工作目录会在调用之间延续——"
    "命令里执行了 cd 并且最终停在工作目录内, 下一次调用会从那个目录继续, "
    "越出工作目录会被重置回工作目录根. 不会保留环境变量或 shell 别名, "
    "命令里的 export 不会影响下一次调用."
)

BASH_PARAMETERS = {
    "type": "object",
    "properties": {
        "command": {"type": "string", "description": "要执行的 shell 命令"},
        "timeout_ms": {
            "type": "integer",
            "description": "超时毫秒数, 默认 120000 (120 秒).",
            "default": 120_000,
        },
    },
    "required": ["command"],
}

BASH_CWD_MARKER = "@@CODING_AGENT_CWD@@"
