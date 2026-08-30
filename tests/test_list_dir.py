"""list_dir：树形列出目录。"""

from __future__ import annotations


def test_lists_tree(env, tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "mod.py").write_text("x = 1\n", encoding="utf-8")
    result = env.registry.execute("list_dir", {"path": "."}, env.ctx)
    assert result.ok
    assert "pkg/" in result.content
    assert "mod.py" in result.content


def test_default_path_is_workspace_root(env, tmp_path):
    (tmp_path / "readme.md").write_text("hi\n", encoding="utf-8")
    result = env.registry.execute("list_dir", {}, env.ctx)
    assert result.ok
    assert "readme.md" in result.content


def test_empty_directory(env, tmp_path):
    (tmp_path / "empty").mkdir()
    result = env.registry.execute("list_dir", {"path": "empty"}, env.ctx)
    assert result.ok
    assert "(空目录)" in result.content


def test_rejects_file_path(env, tmp_path):
    (tmp_path / "a.py").write_text("x\n", encoding="utf-8")
    result = env.registry.execute("list_dir", {"path": "a.py"}, env.ctx)
    assert not result.ok
    assert "不是目录" in (result.error or "")


def test_skips_noise_directories(env, tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("x\n", encoding="utf-8")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "pkg").mkdir()
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "__pycache__" / "x.pyc").write_text("", encoding="utf-8")
    result = env.registry.execute("list_dir", {"path": ".", "depth": 3}, env.ctx)
    assert result.ok
    assert "src/" in result.content
    assert "node_modules" not in result.content
    assert "__pycache__" not in result.content


def test_skips_cyan_state_dir(env, tmp_path):
    (tmp_path / ".cyan").mkdir()
    (tmp_path / ".cyan" / "agent.log").write_text("secret\n", encoding="utf-8")
    (tmp_path / "src").mkdir()
    result = env.registry.execute("list_dir", {"path": ".", "depth": 3}, env.ctx)
    assert result.ok
    assert "src/" in result.content
    assert ".cyan" not in result.content
    assert "agent.log" not in result.content


def test_depth_one_hides_nested_files(env, tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "deep.py").write_text("x\n", encoding="utf-8")
    result = env.registry.execute("list_dir", {"path": ".", "depth": 1}, env.ctx)
    assert result.ok
    assert "pkg/" in result.content
    assert "deep.py" not in result.content


def test_missing_directory(env):
    result = env.registry.execute("list_dir", {"path": "nope"}, env.ctx)
    assert not result.ok


def test_symlink_outside_workspace_is_not_walked(env, tmp_path):
    outside = tmp_path.parent / f"outside-{tmp_path.name}"
    outside.mkdir()
    (outside / "id_rsa").write_text("PRIVATE\n", encoding="utf-8")
    (tmp_path / "leak").symlink_to(outside)
    result = env.registry.execute("list_dir", {"path": ".", "depth": 3}, env.ctx)
    assert result.ok
    assert "leak@" in result.content
    assert "工作区外" in result.content
    assert "id_rsa" not in result.content
