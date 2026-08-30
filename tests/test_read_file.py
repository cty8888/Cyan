"""read_file：带行号读取、分段与预算截断。"""

from __future__ import annotations

from coding_agent.settings import ToolLimits


def test_reads_with_line_numbers(env, tmp_path):
    (tmp_path / "mod.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    result = env.registry.execute("read_file", {"path": "mod.py"}, env.ctx)
    assert result.ok
    assert "1 | def add" in result.content
    assert env.ctx.workspace_access.has_read((tmp_path / "mod.py").resolve())


def test_empty_file(env, tmp_path):
    (tmp_path / "empty.txt").write_text("", encoding="utf-8")
    result = env.registry.execute("read_file", {"path": "empty.txt"}, env.ctx)
    assert result.ok
    assert "内容为空" in result.content
    assert env.ctx.workspace_access.has_read((tmp_path / "empty.txt").resolve())


def test_offset_past_end(env, tmp_path):
    (tmp_path / "mod.py").write_text("a\nb\n", encoding="utf-8")
    result = env.registry.execute("read_file", {"path": "mod.py", "offset": 999}, env.ctx)
    assert result.ok
    assert "共" in result.content and "没有内容" in result.content
    assert not env.ctx.workspace_access.has_read((tmp_path / "mod.py").resolve())


def test_missing_file(env):
    result = env.registry.execute("read_file", {"path": "nope.py"}, env.ctx)
    assert not result.ok


def test_directory_is_rejected(env, tmp_path):
    (tmp_path / "pkg").mkdir()
    result = env.registry.execute("read_file", {"path": "pkg"}, env.ctx)
    assert not result.ok
    assert "是目录" in (result.error or "")


def test_binary_file_is_rejected(env, tmp_path):
    (tmp_path / "blob.bin").write_bytes(b"\x00\xff\x00hello")
    result = env.registry.execute("read_file", {"path": "blob.bin"}, env.ctx)
    assert not result.ok
    assert "二进制" in (result.error or "")


def test_string_offset_is_coerced(env, tmp_path):
    (tmp_path / "mod.py").write_text("a\nb\nc\n", encoding="utf-8")
    result = env.registry.execute("read_file", {"path": "mod.py", "offset": "2"}, env.ctx)
    assert result.ok
    assert "2 |" in result.content


def test_missing_required_path(env):
    result = env.registry.execute("read_file", {}, env.ctx)
    assert not result.ok
    assert "缺少必填参数" in (result.error or "")


def test_partial_view_when_over_budget(make_env, tmp_path):
    big = "\n".join(f"line {i}" for i in range(2000))
    (tmp_path / "big.txt").write_text(big, encoding="utf-8")
    env = make_env(tools=ToolLimits(max_file_read_chars=200))
    result = env.registry.execute("read_file", {"path": "big.txt"}, env.ctx)
    assert result.ok
    assert "[PARTIAL VIEW]" in result.content
    assert not env.ctx.workspace_access.has_read((tmp_path / "big.txt").resolve())


def test_explicit_limit_over_budget_errors(make_env, tmp_path):
    big = "\n".join(f"line {i}" for i in range(2000))
    (tmp_path / "big.txt").write_text(big, encoding="utf-8")
    env = make_env(tools=ToolLimits(max_file_read_chars=200))
    result = env.registry.execute(
        "read_file", {"path": "big.txt", "offset": 1, "limit": 500}, env.ctx
    )
    assert not result.ok
    assert "调小 limit" in (result.error or "")


def test_small_explicit_limit_does_not_count_as_read(make_env, tmp_path):
    big = "\n".join(f"line {i}" for i in range(2000))
    (tmp_path / "big.txt").write_text(big, encoding="utf-8")
    env = make_env(tools=ToolLimits(max_file_read_chars=200))
    result = env.registry.execute(
        "read_file", {"path": "big.txt", "offset": 1, "limit": 3}, env.ctx
    )
    assert result.ok
    assert "PARTIAL" not in result.content
    assert not env.ctx.workspace_access.has_read((tmp_path / "big.txt").resolve())


def test_offset_slice_does_not_count_as_read(env, tmp_path):
    (tmp_path / "mod.py").write_text("a\nb\nc\n", encoding="utf-8")
    result = env.registry.execute("read_file", {"path": "mod.py", "offset": 2}, env.ctx)
    assert result.ok
    assert not env.ctx.workspace_access.has_read((tmp_path / "mod.py").resolve())


def test_explicit_limit_covering_whole_file_counts_as_read(env, tmp_path):
    (tmp_path / "mod.py").write_text("a\nb\nc\n", encoding="utf-8")
    result = env.registry.execute(
        "read_file", {"path": "mod.py", "offset": 1, "limit": 10}, env.ctx
    )
    assert result.ok
    assert env.ctx.workspace_access.has_read((tmp_path / "mod.py").resolve())
