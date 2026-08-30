"""edit_file：精确替换、唯一性与「先读后改」。"""

from __future__ import annotations


def test_exact_replace(env, tmp_path):
    (tmp_path / "mod.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    env.registry.execute("read_file", {"path": "mod.py"}, env.ctx)
    result = env.registry.execute(
        "edit_file",
        {"path": "mod.py", "old_string": "a - b", "new_string": "a + b"},
        env.ctx,
    )
    assert result.ok
    assert "a + b" in (tmp_path / "mod.py").read_text(encoding="utf-8")
    assert "diff" in result.metadata
    assert "+" in result.metadata["diff"]


def test_missing_old_string(env, tmp_path):
    (tmp_path / "mod.py").write_text("hello\n", encoding="utf-8")
    env.registry.execute("read_file", {"path": "mod.py"}, env.ctx)
    result = env.registry.execute(
        "edit_file",
        {"path": "mod.py", "old_string": "不存在", "new_string": "x"},
        env.ctx,
    )
    assert not result.ok
    assert "找不到" in (result.error or "")


def test_non_unique_match_is_rejected(env, tmp_path):
    (tmp_path / "dup.txt").write_text("x\nx\n", encoding="utf-8")
    env.registry.execute("read_file", {"path": "dup.txt"}, env.ctx)
    result = env.registry.execute(
        "edit_file",
        {"path": "dup.txt", "old_string": "x", "new_string": "y"},
        env.ctx,
    )
    assert not result.ok
    assert "不唯一" in (result.error or "")


def test_replace_all(env, tmp_path):
    (tmp_path / "dup.txt").write_text("x\nx\n", encoding="utf-8")
    env.registry.execute("read_file", {"path": "dup.txt"}, env.ctx)
    result = env.registry.execute(
        "edit_file",
        {"path": "dup.txt", "old_string": "x", "new_string": "y", "replace_all": True},
        env.ctx,
    )
    assert result.ok
    assert (tmp_path / "dup.txt").read_text(encoding="utf-8") == "y\ny\n"
    assert "2 处" in result.content


def test_describe_non_unique_match(env, tmp_path):
    (tmp_path / "dup.txt").write_text("x\nx\n", encoding="utf-8")
    env.registry.execute("read_file", {"path": "dup.txt"}, env.ctx)
    summary, detail, _fmt = env.registry.get("edit_file").describe(
        {"path": "dup.txt", "old_string": "x", "new_string": "y", "replace_all": False},
        tmp_path,
        workspace_access=env.ctx.workspace_access,
    )
    assert "无法编辑" in summary
    assert "不唯一" in (detail or "")


def test_edit_without_read_is_rejected(env, tmp_path):
    (tmp_path / "guarded.py").write_text("x = 1\n", encoding="utf-8")
    result = env.registry.execute(
        "edit_file",
        {"path": "guarded.py", "old_string": "x = 1", "new_string": "x = 2"},
        env.ctx,
    )
    assert not result.ok
    assert "必须先用 read_file" in (result.error or "")


def test_edit_after_write_does_not_require_reread(env, tmp_path):
    env.registry.execute("write_file", {"path": "n.py", "content": "x = 2\n"}, env.ctx)
    result = env.registry.execute(
        "edit_file",
        {"path": "n.py", "old_string": "x = 2", "new_string": "x = 3"},
        env.ctx,
    )
    assert result.ok
    assert (tmp_path / "n.py").read_text(encoding="utf-8") == "x = 3\n"


def test_identical_old_and_new_is_rejected(env, tmp_path):
    (tmp_path / "a.py").write_text("same\n", encoding="utf-8")
    env.registry.execute("read_file", {"path": "a.py"}, env.ctx)
    result = env.registry.execute(
        "edit_file",
        {"path": "a.py", "old_string": "same", "new_string": "same"},
        env.ctx,
    )
    assert not result.ok
    assert "无需编辑" in (result.error or "")


def test_missing_file(env):
    result = env.registry.execute(
        "edit_file",
        {"path": "missing.py", "old_string": "a", "new_string": "b"},
        env.ctx,
    )
    assert not result.ok


def test_bash_write_requires_reread_before_edit(env, tmp_path):
    (tmp_path / "mod.py").write_text("old\n", encoding="utf-8")
    env.registry.execute("read_file", {"path": "mod.py"}, env.ctx)
    env.registry.execute("bash", {"command": "echo new > mod.py"}, env.ctx)
    result = env.registry.execute(
        "edit_file",
        {"path": "mod.py", "old_string": "new", "new_string": "newer"},
        env.ctx,
    )
    assert not result.ok
    assert "必须先用 read_file" in (result.error or "")
