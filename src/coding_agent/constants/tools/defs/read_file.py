"""read_file tool constants."""

READ_FILE_NAME = "read_file"

READ_FILE_DESCRIPTION = (
    "读取文本文件内容, 返回结果带行号 (格式为 `行号 | 内容`). "
    "修改任何文件之前都必须先读取它, write_file/edit_file 会强制检查这一点. "
    "不传 limit 时尝试整篇读取; 文件超过单次读取上限会返回 [PARTIAL VIEW] 提示, "
    "按提示传 offset 续读, 或显式传 limit 分段读取."
)

READ_FILE_PARAMETERS = {
    "type": "object",
    "properties": {
        "path": {"type": "string", "description": "文件路径, 相对于工作目录"},
        "offset": {
            "type": "integer",
            "description": "起始行号 (从 1 开始). 默认从头读.",
            "default": 1,
        },
        "limit": {
            "type": "integer",
            "description": (
                "最多读取的行数. 不传表示尽量整篇读取; "
                "显式传入且该范围超过单次读取上限时会报错, 请调小 limit."
            ),
        },
    },
    "required": ["path"],
}
