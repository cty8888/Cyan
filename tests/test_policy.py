"""声明式规则加载、判定与 local 持久化。"""

from __future__ import annotations

from cyan.security.permissions import PermissionManager
from cyan.security.settings_file import (
    add_local_rule,
    local_settings_path,
    project_settings_path,
    remove_local_rule,
    remove_rule,
    user_settings_path,
)
from cyan.security.types import ApprovalDecision, PermissionMode
from cyan.tools.base import Tool
from cyan.tools.types import ToolCapability, ToolContext, ToolRunResult

from .conftest import eval_perm


def test_allow_rule_skips_approval(env):
    add_local_rule(env.settings.workspace, "allow", "bash(pytest *)")
    env.permissions.reload()
    outcome = eval_perm(env, env.registry.get("bash"), {"command": "pytest -q"})
    assert outcome.kind == "allow"


def test_deny_beats_allow(env):
    add_local_rule(env.settings.workspace, "allow", "bash(git *)")
    add_local_rule(env.settings.workspace, "deny", "bash(git push *)")
    env.permissions.reload()
    outcome = eval_perm(env, env.registry.get("bash"), {"command": "git push origin main"})
    assert outcome.kind == "deny"


def test_ask_is_forced(env):
    add_local_rule(env.settings.workspace, "ask", "bash(ruff *)")
    env.permissions.reload()
    outcome = eval_perm(
        env,
        env.registry.get("bash"),
        {"command": "ruff check ."},
        always_allowed={"exec:ruff"},
    )
    assert outcome.kind == "need_approval"
    assert outcome.request.force is True


def test_compound_allow_requires_every_segment(env):
    add_local_rule(env.settings.workspace, "allow", "bash(git status *)")
    env.permissions.reload()
    outcome = eval_perm(
        env, env.registry.get("bash"), {"command": "git status && git commit -m x"}
    )
    assert outcome.kind == "need_approval"


def test_always_allow_persists_bash_to_local(env):
    remembered: set[str] = set()
    env.permissions.apply_decision(
        ApprovalDecision.ALLOW_ALWAYS,
        env.registry.get("bash"),
        {"command": "pytest -q"},
        remembered,
    )
    assert remembered == {"exec:pytest"}
    text = local_settings_path(env.settings.workspace).read_text(encoding="utf-8")
    assert "Bash(pytest *)" in text
    env.permissions.reload()
    outcome = eval_perm(env, env.registry.get("bash"), {"command": "pytest tests"})
    assert outcome.kind == "allow"


def test_write_always_allow_does_not_persist(env):
    remembered: set[str] = set()
    env.permissions.apply_decision(
        ApprovalDecision.ALLOW_ALWAYS,
        env.registry.get("write_file"),
        {"path": "pkg/a.py", "content": "x"},
        remembered,
    )
    assert remembered == {"write:pkg"}
    path = local_settings_path(env.settings.workspace)
    assert not path.is_file()


def test_remove_local_rule(env):
    add_local_rule(env.settings.workspace, "allow", "bash(ruff *)")
    assert remove_local_rule(env.settings.workspace, "bash(ruff *)")
    env.permissions.reload()
    outcome = eval_perm(env, env.registry.get("bash"), {"command": "ruff check"})
    assert outcome.kind == "need_approval"


def test_remove_project_rule(env):
    workspace = env.settings.workspace
    path = project_settings_path(workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        '{"permissions": {"allow": ["bash(ruff *)"]}}\n',
        encoding="utf-8",
    )
    env.permissions.reload()
    assert eval_perm(env, env.registry.get("bash"), {"command": "ruff check"}).kind == "allow"
    status, source = remove_rule(workspace, "bash(ruff *)")
    assert status == "removed"
    assert source == "project"
    env.permissions.reload()
    assert eval_perm(env, env.registry.get("bash"), {"command": "ruff check"}).kind == "need_approval"


def test_remove_user_rule(tmp_path):
    workspace = tmp_path / "ws"
    home = tmp_path / "home"
    workspace.mkdir()
    home.mkdir()
    user_settings_path(home).write_text(
        '{"permissions": {"allow": ["bash(ruff *)"]}}\n',
        encoding="utf-8",
    )
    manager = PermissionManager(workspace, home=home)
    assert any(rule.raw == "bash(ruff *)" and rule.source == "user" for rule in manager.ruleset.rules)
    status, source = remove_rule(workspace, "bash(ruff *)", home=home)
    assert status == "removed"
    assert source == "user"
    manager.reload()
    assert all(rule.raw != "bash(ruff *)" for rule in manager.ruleset.rules)


def test_cannot_remove_builtin_rule(env):
    status, source = remove_rule(env.settings.workspace, "Bash(sudo *)")
    assert status == "builtin"
    assert source == "builtin"


def test_remove_prefers_local_over_project(env):
    workspace = env.settings.workspace
    add_local_rule(workspace, "allow", "bash(ruff *)")
    path = project_settings_path(workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        '{"permissions": {"allow": ["bash(ruff *)"]}}\n',
        encoding="utf-8",
    )
    status, source = remove_rule(workspace, "bash(ruff *)")
    assert status == "removed"
    assert source == "local"
    assert "bash(ruff *)" in path.read_text(encoding="utf-8")


def test_bare_write_deny_hides_write_tools(env):
    add_local_rule(env.settings.workspace, "deny", "write")
    env.permissions.reload()
    assert "write_file" in env.permissions.hidden_tool_names()
    assert "edit_file" in env.permissions.hidden_tool_names()


def test_tests_do_not_read_real_home(tmp_path, monkeypatch):
    monkeypatch.setenv("CYAN_HOME", str(tmp_path / "real-home"))
    (tmp_path / "real-home").mkdir()
    (tmp_path / "real-home" / "settings.json").write_text(
        '{"permissions": {"deny": ["bash"]}}',
        encoding="utf-8",
    )
    manager = PermissionManager(tmp_path)
    assert "bash" not in manager.hidden_tool_names()


def test_plan_still_blocks_write_even_with_allow(env):
    add_local_rule(env.settings.workspace, "allow", "Edit(src/**)")
    env.permissions.reload()
    outcome = eval_perm(
        env,
        env.registry.get("write_file"),
        {"path": "src/a.py", "content": "x"},
        mode=PermissionMode.PLAN,
    )
    assert outcome.kind == "deny"
    assert outcome.deny_reason.value == "mode"


def test_read_deny_blocks_write(env):
    add_local_rule(env.settings.workspace, "deny", "read(notes.md)")
    env.permissions.reload()
    outcome = eval_perm(
        env, env.registry.get("write_file"), {"path": "notes.md", "content": "x"}
    )
    assert outcome.kind == "deny"


def test_unsafe_env_prefix_does_not_match_allow(env):
    add_local_rule(env.settings.workspace, "allow", "bash(ruff *)")
    env.permissions.reload()
    outcome = eval_perm(env, env.registry.get("bash"), {"command": "FOO=bar ruff check"})
    assert outcome.kind == "need_approval"


def test_safe_env_prefix_matches_allow(env):
    add_local_rule(env.settings.workspace, "allow", "bash(ruff *)")
    env.permissions.reload()
    outcome = eval_perm(env, env.registry.get("bash"), {"command": "NODE_ENV=test ruff check"})
    assert outcome.kind == "allow"


def test_deny_peels_any_env_prefix(env):
    add_local_rule(env.settings.workspace, "deny", "bash(ruff *)")
    env.permissions.reload()
    outcome = eval_perm(env, env.registry.get("bash"), {"command": "FOO=bar ruff check"})
    assert outcome.kind == "deny"


def test_deny_src_matches_nested(env):
    add_local_rule(env.settings.workspace, "deny", "Edit(src/**)")
    env.permissions.reload()
    outcome = eval_perm(
        env,
        env.registry.get("write_file"),
        {"path": "vendor/pkg/src/lib.py", "content": "x"},
    )
    assert outcome.kind == "deny"


def test_slash_deny_does_not_match_nested(env):
    add_local_rule(env.settings.workspace, "deny", "Edit(/src/**)")
    env.permissions.reload()
    nested = eval_perm(
        env,
        env.registry.get("write_file"),
        {"path": "vendor/pkg/src/lib.py", "content": "x"},
    )
    root = eval_perm(
        env,
        env.registry.get("write_file"),
        {"path": "src/a.py", "content": "x"},
    )
    assert nested.kind != "deny"
    assert root.kind == "deny"


def test_user_slash_rule_does_not_hit_workspace_path(tmp_path):
    workspace = tmp_path / "ws"
    home = tmp_path / "home"
    workspace.mkdir()
    home.mkdir()
    (workspace / "secrets").mkdir()
    user_settings_path(home).write_text(
        '{"permissions": {"deny": ["Edit(/secrets/**)"]}}\n',
        encoding="utf-8",
    )
    manager = PermissionManager(workspace, home=home)
    from cyan.tools.registry import build_default_registry
    from cyan.security.types import PermissionMode

    registry = build_default_registry()
    outcome = manager.evaluate(
        registry.get("write_file"),
        {"path": "secrets/a.txt", "content": "x"},
        mode=PermissionMode.DEFAULT,
        always_allowed=set(),
    )
    assert outcome.kind != "deny"


def test_write_path_rule_is_inert(env):
    add_local_rule(env.settings.workspace, "allow", "Write(src/**)")
    env.permissions.reload()
    assert any("请改用 Edit" in warning for warning in (env.permissions.ruleset.warnings or []))
    outcome = eval_perm(
        env,
        env.registry.get("write_file"),
        {"path": "src/a.py", "content": "x"},
    )
    assert outcome.kind == "need_approval"


def test_edit_path_rule_matches(env):
    add_local_rule(env.settings.workspace, "allow", "Edit(src/**)")
    env.permissions.reload()
    outcome = eval_perm(
        env,
        env.registry.get("write_file"),
        {"path": "src/a.py", "content": "x"},
    )
    assert outcome.kind == "allow"


class _FakeFetch(Tool):
    name = "webfetch"
    description = "test fetch"
    capability = ToolCapability.READ
    parameters = {
        "type": "object",
        "properties": {"url": {"type": "string"}},
        "required": ["url"],
    }

    def run(self, ctx: ToolContext, **kwargs) -> ToolRunResult:
        return ToolRunResult.success("ok")


class _FakeFetchExec(_FakeFetch):
    capability = ToolCapability.EXEC


def test_deny_bash_timeout_param(env):
    add_local_rule(env.settings.workspace, "deny", "Bash(timeout_ms:1)")
    env.permissions.reload()
    blocked = eval_perm(
        env, env.registry.get("bash"), {"command": "echo hi", "timeout_ms": 1}
    )
    ok = eval_perm(env, env.registry.get("bash"), {"command": "echo hi", "timeout_ms": 120000})
    assert blocked.kind == "deny"
    assert ok.kind == "allow"


def test_allow_timeout_param_does_not_grant(env):
    add_local_rule(env.settings.workspace, "allow", "Bash(timeout_ms:1)")
    env.permissions.reload()
    assert any("不会放行整次调用" in warning for warning in (env.permissions.ruleset.warnings or []))
    outcome = eval_perm(
        env, env.registry.get("bash"), {"command": "touch a.txt", "timeout_ms": 1}
    )
    assert outcome.kind == "need_approval"


def test_bash_command_param_is_ignored(env):
    add_local_rule(env.settings.workspace, "deny", "Bash(command:rm *)")
    env.permissions.reload()
    assert any("复合命令" in warning for warning in (env.permissions.ruleset.warnings or []))
    outcome = eval_perm(env, env.registry.get("bash"), {"command": "rm a.txt"})
    assert outcome.kind != "deny"


def test_webfetch_domain_deny(env):
    add_local_rule(env.settings.workspace, "deny", "WebFetch(domain:evil.com)")
    env.permissions.reload()
    fetch = _FakeFetch()
    blocked = eval_perm(env, fetch, {"url": "https://evil.com/x"})
    ok = eval_perm(env, fetch, {"url": "https://example.com/x"})
    assert blocked.kind == "deny"
    assert ok.kind == "allow"


def test_webfetch_domain_allow(env):
    add_local_rule(env.settings.workspace, "allow", "WebFetch(domain:example.com)")
    env.permissions.reload()
    fetch = _FakeFetchExec()
    outcome = eval_perm(env, fetch, {"url": "https://example.com/page"})
    other = eval_perm(env, fetch, {"url": "https://evil.com/page"})
    assert outcome.kind == "allow"
    assert other.kind == "need_approval"


def test_deny_webfetch_hides_tool(env):
    add_local_rule(env.settings.workspace, "deny", "WebFetch")
    env.permissions.reload()
    assert "webfetch" in env.permissions.hidden_tool_names()
