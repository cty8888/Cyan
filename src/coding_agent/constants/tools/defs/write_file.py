"""write_file tool constants."""

WRITE_FILE_NAME = "write_file"

WRITE_FILE_DESCRIPTION = (
    "把内容整体写入文件, 文件不存在则创建 (父目录会自动创建). "
    "修改已有文件的局部内容时应优先使用 edit_file, 只有新建文件或整体重写时才用本工具. "
    "覆写已存在的文件之前必须先用 read_file 读过它, 否则会被拒绝."
)

WRITE_FILE_PARAMETERS = {
    "type": "object",
    "properties": {
        "path": {"type": "string", "description": "文件路径, 相对于工作目录"},
        "content": {"type": "string", "description": "写入的完整内容"},
    },
    "required": ["path", "content"],
}
