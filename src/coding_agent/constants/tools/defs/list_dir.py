"""list_dir tool constants."""

LIST_DIR_NAME = "list_dir"

LIST_DIR_DESCRIPTION = (
    "列出目录内容, 以树形结构返回. 用于了解项目结构. "
    "会自动跳过 .git, node_modules, __pycache__ 等噪声目录."
)

LIST_DIR_PARAMETERS = {
    "type": "object",
    "properties": {
        "path": {
            "type": "string",
            "description": "目录路径, 相对于工作目录. 默认为工作目录根.",
            "default": ".",
        },
        "depth": {
            "type": "integer",
            "description": "递归深度, 1 表示只列出当前层. 默认 2.",
            "default": 2,
        },
    },
}

LIST_DIR_SKIP_DIRS = {
    ".git",
    ".hg",
    ".svn",
    "node_modules",
    ".venv",
    "venv",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "dist",
    "build",
    ".idea",
    ".vscode",
}
