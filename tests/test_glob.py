"""glob：按文件名模式查找，不尊重 .gitignore。"""

from __future__ import annotations

import os

from cyan.settings import ToolLimits


def test_recursive_py(env, tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "mod.py").write_text("x\n", encoding="utf-8")
    (tmp_path / "root.py").write_text("y\n", encoding="utf-8")
    (tmp_path / "skip.txt").write_text("z\n", encoding="utf-8")
    result = env.registry.execute("glob", {"pattern": "**/*.py"}, env.ctx)
    assert result.ok
    assert "pkg/mod.py" in result.content
    assert "root.py" in result.content
    assert "skip.txt" not in result.content


def test_brace_expansion(env, tmp_path):
    (tmp_path / "a.json").write_text("{}\n", encoding="utf-8")
    (tmp_path / "b.yaml").write_text("x: 1\n", encoding="utf-8")
    (tmp_path / "c.txt").write_text("no\n", encoding="utf-8")
    result = env.registry.execute("glob", {"pattern": "*.{json,yaml}"}, env.ctx)
    assert result.ok
    assert "a.json" in result.content
    assert "b.yaml" in result.content
    assert "c.txt" not in result.content


def test_mtime_newest_first(env, tmp_path):
    older = tmp_path / "older.py"
    newer = tmp_path / "newer.py"
    older.write_text("old\n", encoding="utf-8")
    newer.write_text("new\n", encoding="utf-8")
    os.utime(older, (1_000_000, 1_000_000))
    os.utime(newer, (2_000_000, 2_000_000))
    result = env.registry.execute("glob", {"pattern": "*.py"}, env.ctx)
    assert result.ok
    lines = [line for line in result.content.splitlines() if line.endswith(".py")]
    assert lines[0] == "newer.py"
    assert lines[1] == "older.py"


def test_truncates_at_limit(make_env, tmp_path):
    env = make_env(tools=ToolLimits(max_glob_results=3))
    for index in range(5):
        (tmp_path / f"f{index}.py").write_text("x\n", encoding="utf-8")
    result = env.registry.execute("glob", {"pattern": "*.py"}, env.ctx)
    assert result.ok
    assert "truncated" in result.content
    assert "showing 3 of 5" in result.content
    assert result.metadata.get("truncated") is True
    assert result.metadata.get("match_count") == 3


def test_rejects_nul_in_pattern(env):
    result = env.registry.execute("glob", {"pattern": "a\x00.py"}, env.ctx)
    assert not result.ok
    assert "空字节" in (result.error or "")


def test_rejects_nul_in_path(env):
    result = env.registry.execute("glob", {"pattern": "*.py", "path": "src\x00"}, env.ctx)
    assert not result.ok
    assert "空字节" in (result.error or "")


def test_skips_symlink_outside_workspace(env, tmp_path):
    outside = tmp_path.parent / f"outside-{tmp_path.name}"
    outside.mkdir()
    (outside / "secret.py").write_text("PRIVATE\n", encoding="utf-8")
    (tmp_path / "leak.py").symlink_to(outside / "secret.py")
    (tmp_path / "ok.py").write_text("ok\n", encoding="utf-8")
    result = env.registry.execute("glob", {"pattern": "**/*.py"}, env.ctx)
    assert result.ok
    assert "ok.py" in result.content
    assert "leak.py" not in result.content
    assert "secret.py" not in result.content


def test_finds_gitignored_file(env, tmp_path):
    (tmp_path / ".gitignore").write_text("ignored.py\n", encoding="utf-8")
    (tmp_path / "ignored.py").write_text("x\n", encoding="utf-8")
    (tmp_path / "kept.py").write_text("y\n", encoding="utf-8")
    result = env.registry.execute("glob", {"pattern": "**/*.py"}, env.ctx)
    assert result.ok
    assert "ignored.py" in result.content
    assert "kept.py" in result.content


def test_skips_git_directory(env, tmp_path):
    git = tmp_path / ".git"
    git.mkdir()
    (git / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (tmp_path / "app.py").write_text("x\n", encoding="utf-8")
    result = env.registry.execute("glob", {"pattern": "**/*"}, env.ctx)
    assert result.ok
    assert "app.py" in result.content
    assert ".git/HEAD" not in result.content


def test_skips_sensitive_files(env, tmp_path):
    (tmp_path / ".env").write_text("SECRET=1\n", encoding="utf-8")
    (tmp_path / "app.py").write_text("x\n", encoding="utf-8")
    result = env.registry.execute("glob", {"pattern": "**/*"}, env.ctx)
    assert result.ok
    assert "app.py" in result.content
    assert ".env" not in result.content.splitlines()
    assert "skipped 1 sensitive files" in result.content


def test_glob_does_not_skip_vscode(env, tmp_path):
    vscode = tmp_path / ".vscode"
    vscode.mkdir()
    (vscode / "settings.json").write_text("{}\n", encoding="utf-8")
    result = env.registry.execute("glob", {"pattern": "**/*"}, env.ctx)
    assert result.ok
    assert ".vscode/settings.json" in result.content
    (tmp_path / ".env").write_text("SECRET=1\n", encoding="utf-8")
    (tmp_path / "app.py").write_text("x\n", encoding="utf-8")
    result = env.registry.execute("glob", {"pattern": "**/*"}, env.ctx)
    assert result.ok
    assert "app.py" in result.content
    assert ".env" not in result.content.splitlines()
    assert "skipped 1 sensitive files" in result.content


def test_ssh_directory_search_drops_keys(env, tmp_path):
    ssh = tmp_path / ".ssh"
    ssh.mkdir()
    (ssh / "id_rsa").write_text("PRIVATE KEY\n", encoding="utf-8")
    result = env.registry.execute("glob", {"pattern": "*", "path": ".ssh"}, env.ctx)
    assert result.ok
    assert "id_rsa" not in result.content
    assert "skipped 1 sensitive files" in result.content


def test_rejects_file_as_search_root(env, tmp_path):
    (tmp_path / "a.py").write_text("x\n", encoding="utf-8")
    result = env.registry.execute("glob", {"pattern": "*.py", "path": "a.py"}, env.ctx)
    assert not result.ok
    assert "不是目录" in (result.error or "")


def test_search_under_subdirectory(env, tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "mod.py").write_text("x\n", encoding="utf-8")
    (tmp_path / "root.py").write_text("y\n", encoding="utf-8")
    result = env.registry.execute("glob", {"pattern": "*.py", "path": "pkg"}, env.ctx)
    assert result.ok
    assert "pkg/mod.py" in result.content
    assert "root.py" not in result.content


def test_no_files_found(env, tmp_path):
    (tmp_path / "a.txt").write_text("x\n", encoding="utf-8")
    result = env.registry.execute("glob", {"pattern": "**/*.py"}, env.ctx)
    assert result.ok
    assert result.content == "No files found"
