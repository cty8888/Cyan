"""edit_file tool constants."""

EDIT_FILE_NAME = "edit_file"

EDIT_FILE_DESCRIPTION = (
    "通过精确字符串替换修改文件的局部内容, 比整文件重写更省 token, 是修改已有文件的首选方式. "
    "old_string 必须与文件中的内容逐字符完全一致 (含缩进), "
    "且在文件中唯一——如果不唯一, 请多带几行上下文使其唯一, 或设置 replace_all=true. "
    "编辑前必须先用 read_file 读过该文件, 否则会被拒绝."
)

EDIT_FILE_PARAMETERS = {
    "type": "object",
    "properties": {
        "path": {"type": "string", "description": "文件路径, 相对于工作目录"},
        "old_string": {"type": "string", "description": "要被替换的原文, 必须完全匹配且唯一"},
        "new_string": {"type": "string", "description": "替换后的新内容, 留空表示删除"},
        "replace_all": {
            "type": "boolean",
            "description": "是否替换所有匹配项, 默认 false (要求唯一匹配)",
            "default": False,
        },
    },
    "required": ["path", "old_string", "new_string"],
}
