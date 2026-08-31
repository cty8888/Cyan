"""声明式权限规则语法。"""

from __future__ import annotations

import pytest

from cyan.security.rule_syntax import (
    bash_subjects,
    domain_from_args,
    match_bash_pattern,
    match_path_pattern,
    parse_rule,
    unwrap_segment_text,
)


def test_parse_bare_family():
    parsed = parse_rule("bash")
    assert parsed.family == "bash"
    assert parsed.pattern is None


def test_parse_pattern():
    parsed = parse_rule("bash(pytest *)")
    assert parsed.family == "bash"
    assert parsed.pattern == "pytest *"


def test_parse_rejects_unknown():
    with pytest.raises(ValueError, match="未知规则名"):
        parse_rule("???")


def test_parse_webfetch_domain():
    parsed = parse_rule("WebFetch(domain:example.com)")
    assert parsed.family == "webfetch"
    assert parsed.param == "domain"
    assert parsed.pattern == "example.com"
    assert parsed.tool_name == "webfetch"
    assert parsed.inert is False


def test_parse_bash_timeout_param():
    parsed = parse_rule("Bash(timeout_ms:120000)")
    assert parsed.family == "bash"
    assert parsed.param == "timeout_ms"
    assert parsed.pattern == "120000"
    assert parsed.tool_name == "bash"
    assert parsed.inert is False


def test_parse_bash_command_param_is_inert():
    parsed = parse_rule("Bash(command:rm *)")
    assert parsed.param == "command"
    assert parsed.inert is True


def test_parse_read_path_param_is_inert():
    parsed = parse_rule("Read(path:.env)")
    assert parsed.inert is True
    assert parsed.param == "path"


def test_parse_edit_and_write_alias():
    assert parse_rule("Edit(src/**)").family == "write"
    assert parse_rule("Edit(src/**)").inert is False
    assert parse_rule("Write(.env)").family == "write"
    assert parse_rule("Write(.env)").inert is True
    assert parse_rule("Write").inert is False
    assert parse_rule("Bash(pytest *)").family == "bash"
    assert parse_rule("Read(.env)").family == "read"


def test_bash_trailing_star_matches_bare_command():
    assert match_bash_pattern("pytest *", "pytest")
    assert match_bash_pattern("pytest *", "pytest -q")
    assert not match_bash_pattern("pytest *", "pytestfoo")


def test_bash_exact_does_not_match_suffix():
    assert match_bash_pattern("npm run build", "npm run build")
    assert not match_bash_pattern("npm run build", "npm run build --watch")


def test_unwrap_timeout():
    assert unwrap_segment_text("timeout 30 pytest -q") == "pytest -q"


def test_unwrap_assignment_for_deny():
    assert unwrap_segment_text("FOO=bar pytest -q") == "pytest -q"
    assert unwrap_segment_text("FOO=bar pytest -q", peel_all_assignments=False) == "FOO=bar pytest -q"
    assert unwrap_segment_text("NODE_ENV=test pytest -q", peel_all_assignments=False) == "pytest -q"


def test_compound_subjects():
    subjects = bash_subjects("git status && git push origin main")
    assert subjects == ["git status", "git push origin main"]


def test_path_deny_bare_name_any_depth():
    assert match_path_pattern(".env", ".env", deep=True)
    assert match_path_pattern(".env", "pkg/.env", deep=True)
    assert not match_path_pattern(".env", ".envoy", deep=True)


def test_path_allow_is_root_anchored():
    assert match_path_pattern("src/**", "src/a.py", deep=False)
    assert not match_path_pattern("src/**", "vendor/src/a.py", deep=False)


def test_path_deny_single_dir_any_depth():
    assert match_path_pattern("src/**", "src/a.py", deep=True)
    assert match_path_pattern("src/**", "vendor/pkg/src/lib.js", deep=True)
    assert not match_path_pattern("src/components/**", "vendor/src/components/a.ts", deep=True)


def test_git_dir_glob():
    assert match_path_pattern(".git/**", ".git/config", deep=True)
    assert not match_path_pattern(".git/**", ".git", deep=True)
    assert not match_path_pattern(".git/**", "src/git/config", deep=True)
    assert match_path_pattern(".git/**", "vendor/.git/config", deep=True)


def test_settings_slash_is_root_anchored_not_any_depth():
    assert match_path_pattern("/src/**", "src/a.py", deep=True)
    assert not match_path_pattern("/src/**", "vendor/pkg/src/lib.js", deep=True)


def test_abs_anchor_matches_filesystem_path(tmp_path):
    target = tmp_path / "secrets" / "a.key"
    target.parent.mkdir()
    target.write_text("x\n", encoding="utf-8")
    assert match_path_pattern(
        f"//{tmp_path.as_posix()}/secrets/**",
        "ignored",
        deep=False,
        absolute=target,
    )
    assert not match_path_pattern(
        "//tmp/other/**",
        "ignored",
        deep=False,
        absolute=target,
    )


def test_home_anchor_matches_under_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    doc = home / "Documents" / "a.pdf"
    doc.parent.mkdir(parents=True)
    doc.write_text("x\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr("cyan.security.rule_syntax.Path.home", lambda: home)
    assert match_path_pattern("~/Documents/*.pdf", "ignored", deep=False, absolute=doc)
    assert not match_path_pattern("~/Downloads/*.pdf", "ignored", deep=False, absolute=doc)


def test_user_settings_slash_anchors_to_config_home(tmp_path):
    home = tmp_path / "cyan-home"
    workspace = tmp_path / "ws"
    locked = home / "secrets" / "a.txt"
    other = workspace / "secrets" / "a.txt"
    locked.parent.mkdir(parents=True)
    other.parent.mkdir(parents=True)
    locked.write_text("x\n", encoding="utf-8")
    other.write_text("x\n", encoding="utf-8")
    assert match_path_pattern(
        "/secrets/**",
        "secrets/a.txt",
        deep=True,
        absolute=locked,
        workspace=workspace,
        config_home=home,
        source="user",
    )
    assert not match_path_pattern(
        "/secrets/**",
        "secrets/a.txt",
        deep=True,
        absolute=other,
        workspace=workspace,
        config_home=home,
        source="user",
    )


def test_domain_from_url():
    assert domain_from_args({"url": "https://evil.com/x"}) == "evil.com"
    assert domain_from_args({"url": "evil.com/x"}) == "evil.com"
    assert domain_from_args({"domain": "Example.COM"}) == "example.com"
    assert domain_from_args({}) is None
