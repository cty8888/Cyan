"""grep：基于 ripgrep 搜内容；无 rg 时跳过需要它的用例。"""

from __future__ import annotations

import shutil
import subprocess

import pytest


def _require_rg() -> None:
    if shutil.which("rg") is None:
        pytest.skip("需要 ripgrep (rg)")


def _git_init(tmp_path) -> None:
    if shutil.which("git") is None:
        pytest.skip("需要 git")
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True, text=True)


def test_files_with_matches_is_default(env, tmp_path):
    _require_rg()
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "mod.py").write_text("hello world\n", encoding="utf-8")
    (tmp_path / "other.py").write_text("nothing\n", encoding="utf-8")
    result = env.registry.execute("grep", {"pattern": "hello"}, env.ctx)
    assert result.ok
    assert "pkg/mod.py" in result.content
    assert "other.py" not in result.content
    assert "hello world" not in result.content


def test_content_includes_line_numbers(env, tmp_path):
    _require_rg()
    (tmp_path / "app.py").write_text("alpha\nhello world\nomega\n", encoding="utf-8")
    result = env.registry.execute(
        "grep", {"pattern": "hello", "output_mode": "content"}, env.ctx
    )
    assert result.ok
    assert "app.py:2:hello world" in result.content


def test_count_includes_total(env, tmp_path):
    _require_rg()
    (tmp_path / "a.py").write_text("foo\nfoo\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("foo\n", encoding="utf-8")
    result = env.registry.execute("grep", {"pattern": "foo", "output_mode": "count"}, env.ctx)
    assert result.ok
    assert "a.py:2" in result.content
    assert "b.py:1" in result.content
    assert "total: 3" in result.content


def test_count_total_not_shrunk_by_head_limit(env, tmp_path):
    _require_rg()
    (tmp_path / "a.py").write_text("foo\nfoo\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("foo\n", encoding="utf-8")
    result = env.registry.execute(
        "grep",
        {"pattern": "foo", "output_mode": "count", "head_limit": 1},
        env.ctx,
    )
    assert result.ok
    listed = [line for line in result.content.splitlines() if line.startswith("total:") is False]
    assert len(listed) == 1
    assert "total: 3" in result.content


def test_glob_filter(env, tmp_path):
    _require_rg()
    (tmp_path / "keep.py").write_text("needle\n", encoding="utf-8")
    (tmp_path / "skip.txt").write_text("needle\n", encoding="utf-8")
    result = env.registry.execute("grep", {"pattern": "needle", "glob": "*.py"}, env.ctx)
    assert result.ok
    assert "keep.py" in result.content
    assert "skip.txt" not in result.content


def test_type_filter(env, tmp_path):
    _require_rg()
    (tmp_path / "keep.py").write_text("needle\n", encoding="utf-8")
    (tmp_path / "skip.txt").write_text("needle\n", encoding="utf-8")
    result = env.registry.execute("grep", {"pattern": "needle", "type": "py"}, env.ctx)
    assert result.ok
    assert "keep.py" in result.content
    assert "skip.txt" not in result.content


def test_gitignore_skips_ignored_file(env, tmp_path):
    _require_rg()
    _git_init(tmp_path)
    (tmp_path / ".gitignore").write_text("ignored.py\n", encoding="utf-8")
    (tmp_path / "ignored.py").write_text("SECRET_TOKEN\n", encoding="utf-8")
    (tmp_path / "kept.py").write_text("SECRET_TOKEN\n", encoding="utf-8")
    result = env.registry.execute("grep", {"pattern": "SECRET_TOKEN"}, env.ctx)
    assert result.ok
    assert "kept.py" in result.content
    assert "ignored.py" not in result.content


def test_explicit_path_searches_gitignored_file(env, tmp_path):
    _require_rg()
    _git_init(tmp_path)
    (tmp_path / ".gitignore").write_text("ignored.py\n", encoding="utf-8")
    (tmp_path / "ignored.py").write_text("SECRET_TOKEN\n", encoding="utf-8")
    result = env.registry.execute(
        "grep", {"pattern": "SECRET_TOKEN", "path": "ignored.py"}, env.ctx
    )
    assert result.ok
    assert "ignored.py" in result.content


def test_invalid_regex_returns_rg_diagnostic(env, tmp_path):
    _require_rg()
    (tmp_path / "a.py").write_text("x\n", encoding="utf-8")
    result = env.registry.execute("grep", {"pattern": "("}, env.ctx)
    assert not result.ok
    assert result.error
    lowered = result.error.lower()
    assert "regex" in lowered or "unclosed" in lowered or "parse" in lowered


def test_content_offset_past_matches(env, tmp_path):
    _require_rg()
    (tmp_path / "a.py").write_text("hello\n", encoding="utf-8")
    result = env.registry.execute(
        "grep",
        {"pattern": "hello", "output_mode": "content", "offset": 50},
        env.ctx,
    )
    assert result.ok
    assert "No entries at this offset" in result.content


def test_no_match_is_success(env, tmp_path):
    _require_rg()
    (tmp_path / "a.py").write_text("hello\n", encoding="utf-8")
    result = env.registry.execute("grep", {"pattern": "zzzz-not-here"}, env.ctx)
    assert result.ok
    assert result.content == "No files found"


def test_searches_hidden_github_dir(env, tmp_path):
    _require_rg()
    github = tmp_path / ".github"
    github.mkdir()
    (github / "ci.yml").write_text("runs-on: ubuntu\n", encoding="utf-8")
    result = env.registry.execute("grep", {"pattern": "ubuntu"}, env.ctx)
    assert result.ok
    assert ".github/ci.yml" in result.content


def test_skips_sensitive_files_in_workspace_search(env, tmp_path):
    _require_rg()
    (tmp_path / ".env").write_text("SECRET=fromenv\n", encoding="utf-8")
    (tmp_path / "app.py").write_text("SECRET=fromapp\n", encoding="utf-8")
    result = env.registry.execute(
        "grep", {"pattern": "SECRET", "output_mode": "content"}, env.ctx
    )
    assert result.ok
    assert "fromapp" in result.content
    assert "fromenv" not in result.content
    assert "skipped 1 sensitive files" in result.content


def test_content_keeps_dash_number_filename(env, tmp_path):
    _require_rg()
    (tmp_path / "issue-123-fix.py").write_text("TODO fix this\n", encoding="utf-8")
    result = env.registry.execute(
        "grep", {"pattern": "TODO", "output_mode": "content"}, env.ctx
    )
    assert result.ok
    assert "issue-123-fix.py:1:TODO fix this" in result.content


def test_ssh_directory_search_drops_keys(env, tmp_path):
    _require_rg()
    ssh = tmp_path / ".ssh"
    ssh.mkdir()
    (ssh / "id_rsa").write_text("PRIVATE KEY\n", encoding="utf-8")
    result = env.registry.execute(
        "grep", {"pattern": "PRIVATE", "path": ".ssh", "output_mode": "content"}, env.ctx
    )
    assert result.ok
    assert "PRIVATE KEY" not in result.content
    assert "id_rsa" not in result.content
    assert "skipped" in result.content


def test_explicit_env_path_keeps_content(env, tmp_path):
    _require_rg()
    (tmp_path / ".env").write_text("SECRET=fromenv\n", encoding="utf-8")
    result = env.registry.execute(
        "grep",
        {"pattern": "SECRET", "path": ".env", "output_mode": "content"},
        env.ctx,
    )
    assert result.ok
    assert "fromenv" in result.content


def test_case_insensitive(env, tmp_path):
    _require_rg()
    (tmp_path / "a.py").write_text("Hello\n", encoding="utf-8")
    missed = env.registry.execute("grep", {"pattern": "hello"}, env.ctx)
    assert missed.ok
    assert missed.content == "No files found"
    found = env.registry.execute(
        "grep", {"pattern": "hello", "case_insensitive": True}, env.ctx
    )
    assert found.ok
    assert "a.py" in found.content


def test_rejects_nul_in_pattern(env):
    result = env.registry.execute("grep", {"pattern": "a\x00b"}, env.ctx)
    assert not result.ok
    assert "空字节" in (result.error or "")


def test_missing_rg(env, tmp_path, monkeypatch):
    (tmp_path / "a.py").write_text("hello\n", encoding="utf-8")
    monkeypatch.setattr("cyan.tools.builtin.grep.shutil.which", lambda _name: None)
    result = env.registry.execute("grep", {"pattern": "hello"}, env.ctx)
    assert not result.ok
    assert "ripgrep" in (result.error or "")
