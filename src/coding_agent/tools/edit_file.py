"""edit_file —— 精确字符串替换，修改已有文件的首选方式。"""

from __future__ import annotations

from typing import Any

from ..errors import InvalidToolArgumentsError, ToolError
from ..security.policy import SecurityPolicy
from ._diff import unified_diff
from .base import RiskLevel, Tool, ToolContext, ToolResult


class EditFileTool(Tool):
    name = "edit_file"
    description = (
        "通过精确字符串替换修改文件的局部内容，比整文件重写更省 token，是修改已有文件的首选方式。"
        "old_string 必须与文件中的内容逐字符完全一致（含缩进），"
        "且在文件中唯一——如果不唯一，请多带几行上下文使其唯一，或设置 replace_all=true。"
        "编辑前必须先用 read_file 读过该文件，否则会被拒绝。"
    )
    risk = RiskLevel.WRITE
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "文件路径，相对于工作目录"},
            "old_string": {"type": "string", "description": "要被替换的原文，必须完全匹配且唯一"},
            "new_string": {"type": "string", "description": "替换后的新内容，留空表示删除"},
            "replace_all": {
                "type": "boolean",
                "description": "是否替换所有匹配项，默认 false（要求唯一匹配）",
                "default": False,
            },
        },
        "required": ["path", "old_string", "new_string"],
    }

    def describe(self, args: dict[str, Any], policy: SecurityPolicy) -> tuple[str, str | None, str]:
        raw_path = str(args.get("path", ""))
        try:
            target = policy.resolve_path(raw_path)
            original = target.read_text(encoding="utf-8")
        except Exception:
            return f"编辑 {raw_path}", None, "text"

        old_string = str(args.get("old_string", ""))
        new_string = str(args.get("new_string", ""))
        count = -1 if args.get("replace_all") else 1
        updated = original.replace(old_string, new_string, count)
        diff = unified_diff(original, updated, policy.display(target))
        return f"编辑文件 {policy.display(target)}", diff, "diff"

    def run(
        self,
        ctx: ToolContext,
        path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
    ) -> ToolResult:
        target = ctx.policy.resolve_path(path, must_exist=True)
        if not target.is_file():
            raise ToolError(f"{ctx.policy.display(target)} 不是文件")
        if not ctx.session.has_read(target):
            raise ToolError(f"编辑 {ctx.policy.display(target)} 之前必须先用 read_file 读取它的当前内容。")
        if old_string == new_string:
            raise InvalidToolArgumentsError("old_string 与 new_string 相同，无需编辑")

        original = target.read_text(encoding="utf-8")
        occurrences = original.count(old_string)

        if occurrences == 0:
            raise ToolError(
                f"在 {ctx.policy.display(target)} 中找不到 old_string。"
                "请先用 read_file 确认原文（注意缩进、空格和换行必须逐字符一致）。"
            )
        if occurrences > 1 and not replace_all:
            raise ToolError(
                f"old_string 在 {ctx.policy.display(target)} 中出现了 {occurrences} 次，不唯一。"
                "请补充上下文让它唯一，或设置 replace_all=true 替换全部。"
            )

        updated = original.replace(old_string, new_string, -1 if replace_all else 1)
        target.write_text(updated, encoding="utf-8")
        ctx.session.mark_read(target)

        return ToolResult.success(
            f"已修改 {ctx.policy.display(target)}（替换 {occurrences if replace_all else 1} 处）",
            diff=unified_diff(original, updated, ctx.policy.display(target)),
        )
