"""兼容旧导入：命令分析已迁至 ``shell.py``。"""

from .shell import command_head, is_readonly_command

__all__ = ["command_head", "is_readonly_command"]
