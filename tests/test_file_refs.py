"""``extract_file_refs``：从任务文本里挑出 ``@path`` 引用并读出内容。"""

from __future__ import annotations

from cyan.cli.file_refs import extract_file_refs
from cyan.settings import ToolLimits


def test_extracts_existing_file_reference(tmp_path):
    (tmp_path / "a.py").write_text("print(1)", encoding="utf-8")

    refs = extract_file_refs("看看 @a.py 里写了什么", tmp_path)

    assert len(refs) == 1
    assert refs[0].path == "a.py"
    assert refs[0].content == "print(1)"


def test_ignores_nonexistent_path():
    refs = extract_file_refs("联系我 @某人 谢谢", "/tmp")
    assert refs == []


def test_ignores_path_outside_workspace(tmp_path):
    outside = tmp_path.parent / "outside.txt"
    outside.write_text("secret", encoding="utf-8")

    refs = extract_file_refs(f"看看 @{outside}", tmp_path)

    assert refs == []


def test_ignores_directory_reference(tmp_path):
    (tmp_path / "sub").mkdir()

    refs = extract_file_refs("看看 @sub", tmp_path)

    assert refs == []


def test_strips_trailing_chinese_punctuation(tmp_path):
    (tmp_path / "a.py").write_text("x = 1", encoding="utf-8")

    refs = extract_file_refs("看看@a.py。", tmp_path)

    assert len(refs) == 1
    assert refs[0].path == "a.py"


def test_deduplicates_repeated_references(tmp_path):
    (tmp_path / "a.py").write_text("x = 1", encoding="utf-8")

    refs = extract_file_refs("对比 @a.py 和 @a.py", tmp_path)

    assert len(refs) == 1


def test_multiple_distinct_references(tmp_path):
    (tmp_path / "a.py").write_text("a", encoding="utf-8")
    (tmp_path / "b.py").write_text("b", encoding="utf-8")

    refs = extract_file_refs("对比 @a.py 和 @b.py", tmp_path)

    assert [r.path for r in refs] == ["a.py", "b.py"]


def test_respects_max_file_bytes_limit(tmp_path):
    (tmp_path / "big.txt").write_text("x" * 100, encoding="utf-8")

    refs = extract_file_refs("看看 @big.txt", tmp_path, ToolLimits(max_file_bytes=10))

    assert refs == []


def test_truncates_content_over_char_limit(tmp_path):
    (tmp_path / "big.txt").write_text("x" * 100, encoding="utf-8")

    refs = extract_file_refs("看看 @big.txt", tmp_path, ToolLimits(max_file_read_chars=20))

    assert len(refs) == 1
    assert len(refs[0].content) <= 20
    assert refs[0].content.endswith("[truncated]")


def test_no_at_reference_returns_empty_list(tmp_path):
    assert extract_file_refs("普通的任务描述", tmp_path) == []
