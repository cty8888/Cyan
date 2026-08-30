"""编辑用的文本规范化：换行风格、去掉 read_file 行号前缀。"""

from __future__ import annotations

import re
from pathlib import Path

_NUMBERED_LINE = re.compile(r"^[ \t]*\d+[ \t]*\| ?")


def detect_newline(text: str) -> str:
    """文件里实际用的换行。混合时优先 CRLF。"""
    if "\r\n" in text:
        return "\r\n"
    if "\r" in text:
        return "\r"
    return "\n"


def to_lf(text: str) -> str:
    """把 CRLF / CR 收成 \\n，供匹配。"""
    return text.replace("\r\n", "\n").replace("\r", "\n")


def from_lf(text: str, newline: str) -> str:
    """把 \\n 还原成文件原来的换行。"""
    if newline == "\n":
        return text
    return text.replace("\n", newline)


def strip_read_line_prefixes(text: str) -> str:
    """若每一行都像 ``12 | 内容``，去掉行号前缀。有一行不像则原样返回。"""
    lines = text.splitlines()
    if not lines:
        return text
    stripped: list[str] = []
    for line in lines:
        match = _NUMBERED_LINE.match(line)
        if match is None:
            return text
        stripped.append(line[match.end() :])
    ended = text.endswith("\n") or text.endswith("\r")
    body = "\n".join(stripped)
    return body + ("\n" if ended else "")


def read_text(path: Path) -> str:
    """按原文读入，不把 CRLF 折成 \\n。"""
    with path.open("r", encoding="utf-8", errors="replace", newline="") as handle:
        return handle.read()


def write_text(path: Path, text: str) -> None:
    """按字符串原样写出，不按 os.linesep 再转一遍换行。"""
    with path.open("w", encoding="utf-8", newline="") as handle:
        handle.write(text)


def apply_existing_newline(content: str, existing: str) -> str:
    """覆写时：模型给的是 LF，就沿用磁盘换行；模型自己带了 CRLF 则尊重模型。"""
    if detect_newline(content) != "\n":
        return content
    return from_lf(to_lf(content), detect_newline(existing))
