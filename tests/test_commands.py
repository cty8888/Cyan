"""斜杠命令：/stream 直接改 LLMSettings.stream，下一次模型调用立刻生效；
/loop /context /tools /compact /model /status 改的是 Runtime 上的策略副本。
"""

from __future__ import annotations

import io
from pathlib import Path
from types import SimpleNamespace

from rich.console import Console
from rich.text import Text

from cyan.cli.commands import (
    _cmd_changes,
    _cmd_compact,
    _cmd_context,
    _cmd_loop,
    _cmd_model,
    _cmd_resume,
    _cmd_skill,
    _cmd_skills,
    _cmd_status,
    _cmd_stream,
    _cmd_todos,
    _cmd_tools,
)
from cyan.cli.renderer import Renderer
from cyan.context.types import ContextPolicy
from cyan.security.types import PermissionMode
from cyan.session import Session, TodoItem, TodoStatus
from cyan.session.store import DiskStore
from cyan.settings import CompactPolicy, LLMSettings, LoopLimits, ToolLimits


def _fake_app(stream: bool = True) -> SimpleNamespace:
    console = Console(file=io.StringIO(), force_terminal=True, width=80)
    return SimpleNamespace(
        settings=SimpleNamespace(llm=LLMSettings(api_key="k", stream=stream)),
        renderer=Renderer(console),
    )


def _fake_policy_app() -> SimpleNamespace:
    """给 /loop /context /tools /compact /model /status 用的假 app：
    Runtime 侧的策略副本用真实 dataclass（跟生产代码走同一套字段名/类型转换逻辑），
    其余（session/registry）用最小 stub。
    """
    console = Console(file=io.StringIO(), force_terminal=True, width=80)
    session = SimpleNamespace(
        messages=[],
        todos=[],
        stats=lambda: {
            "llm_calls": 2,
            "tool_calls": 1,
            "total_tokens": 100,
        },
        permissions=SimpleNamespace(permission_mode=PermissionMode.DEFAULT),
        metadata=SimpleNamespace(session_id="abcdef1234567890", title="测试会话"),
        workspace=SimpleNamespace(modified_files=set()),
    )
    session.set_todos = lambda items: setattr(session, "todos", items)
    runtime = SimpleNamespace(
        loop_limits=LoopLimits(),
        tool_limits=ToolLimits(),
        context_policy=ContextPolicy(),
        compact_policy=CompactPolicy(),
        estimate_request_tokens=lambda: 500,
        compact=lambda **kwargs: True,
    )
    return SimpleNamespace(
        settings=SimpleNamespace(
            llm=LLMSettings(api_key="k", model="deepseek-chat", stream=True),
            workspace=Path("/tmp/cyan-test-workspace"),
        ),
        renderer=Renderer(console),
        runtime=runtime,
        session=session,
        registry=[],
    )


def _plain(text: str) -> str:
    """去掉 rich 色码，按终端可见字符做断言。

    ``force_terminal=True`` 时 rich 会给数字单独上色，``"改动了 2 个文件"``
    这种整句匹配会被 ANSI 拆开；CI 的非 TTY 环境同样会走到这条路径。
    """
    return Text.from_ansi(text).plain


def _output(app: SimpleNamespace) -> str:
    return _plain(app.renderer.console.file.getvalue())


def test_stream_no_args_only_reports_state():
    app = _fake_app(stream=True)
    assert _cmd_stream(app, []) is False
    assert app.settings.llm.stream is True


def test_stream_off_disables_setting():
    app = _fake_app(stream=True)
    _cmd_stream(app, ["off"])
    assert app.settings.llm.stream is False


def test_stream_on_enables_setting():
    app = _fake_app(stream=False)
    _cmd_stream(app, ["on"])
    assert app.settings.llm.stream is True


def test_stream_invalid_argument_does_not_change_setting():
    app = _fake_app(stream=True)
    _cmd_stream(app, ["maybe"])
    assert app.settings.llm.stream is True


# ---------------------------------------------------------------- /loop


def test_loop_no_args_shows_current_values():
    app = _fake_policy_app()
    assert _cmd_loop(app, []) is False
    assert "max_iterations" in _output(app)


def test_loop_sets_valid_field():
    app = _fake_policy_app()
    _cmd_loop(app, ["max_iterations", "5"])
    assert app.runtime.loop_limits.max_iterations == 5


def test_loop_unknown_field_reports_error_and_does_not_change():
    app = _fake_policy_app()
    _cmd_loop(app, ["nope", "5"])
    assert "没有这个字段" in _output(app)
    assert app.runtime.loop_limits.max_iterations == LoopLimits().max_iterations


def test_loop_bad_type_reports_error_and_does_not_change():
    app = _fake_policy_app()
    _cmd_loop(app, ["max_iterations", "abc"])
    assert "需要" in _output(app)
    assert app.runtime.loop_limits.max_iterations == LoopLimits().max_iterations


# ---------------------------------------------------------------- /context


def test_context_no_args_shows_current_values():
    app = _fake_policy_app()
    assert _cmd_context(app, []) is False
    assert "max_tool_result_chars" in _output(app)


def test_context_sets_valid_field():
    app = _fake_policy_app()
    _cmd_context(app, ["max_tool_result_chars", "1000"])
    assert app.runtime.context_policy.max_tool_result_chars == 1000


# ---------------------------------------------------------------- /tools


def test_tools_no_args_lists_registered_tools():
    app = _fake_policy_app()
    app.registry = [SimpleNamespace(name="bash", capability=SimpleNamespace(value="exec"), description="跑命令")]
    _cmd_tools(app, [])
    assert "bash" in _output(app)


def test_tools_limits_shows_tool_limits():
    app = _fake_policy_app()
    _cmd_tools(app, ["limits"])
    assert "max_file_read_chars" in _output(app)


def test_tools_sets_valid_field():
    app = _fake_policy_app()
    _cmd_tools(app, ["max_dir_entries", "10"])
    assert app.runtime.tool_limits.max_dir_entries == 10


# ---------------------------------------------------------------- /compact


def test_compact_show_prints_policy():
    app = _fake_policy_app()
    _cmd_compact(app, ["show"])
    assert "max_context_tokens" in _output(app)


def test_compact_set_updates_field():
    app = _fake_policy_app()
    _cmd_compact(app, ["set", "keep_recent_turns", "5"])
    assert app.runtime.compact_policy.keep_recent_turns == 5


def test_compact_no_args_still_triggers_immediate_compaction():
    """扩展子命令不能破坏原有的「不带参数立即压缩」行为。"""
    from cyan.llm.types import AssistantMessage, UserMessage

    app = _fake_policy_app()
    app.session.messages = [UserMessage.of("问题"), AssistantMessage.of("回答")] * 4
    called = {}
    app.runtime.compact = lambda **kwargs: called.setdefault("done", True) or True
    _cmd_compact(app, [])
    assert called.get("done") is True


# ---------------------------------------------------------------- /model


def test_model_no_args_reports_current_model():
    app = _fake_policy_app()
    _cmd_model(app, [])
    assert "deepseek-chat" in _output(app)


def test_model_with_arg_switches_model():
    app = _fake_policy_app()
    _cmd_model(app, ["deepseek-reasoner"])
    assert app.settings.llm.model == "deepseek-reasoner"


# ---------------------------------------------------------------- /status


def test_status_reports_summary():
    app = _fake_policy_app()
    _cmd_status(app, [])
    output = _output(app)
    assert "deepseek-chat" in output
    assert "500" in output


# ---------------------------------------------------------------- /todos


def test_todos_no_args_reports_empty_list():
    app = _fake_policy_app()
    assert _cmd_todos(app, []) is False
    assert "没有任务清单" in _output(app)


def test_todos_no_args_lists_current_items():
    app = _fake_policy_app()
    app.session.todos = [
        TodoItem(content="写测试", status=TodoStatus.IN_PROGRESS, active_form="正在写测试"),
        TodoItem(content="补文档", status=TodoStatus.PENDING),
    ]
    _cmd_todos(app, [])
    output = _output(app)
    assert "正在写测试" in output
    assert "补文档" in output


def test_todos_clear_empties_list():
    app = _fake_policy_app()
    app.session.todos = [TodoItem(content="写测试")]
    _cmd_todos(app, ["clear"])
    assert app.session.todos == []
    assert "已清空" in _output(app)


def test_todos_unknown_argument_reports_usage():
    app = _fake_policy_app()
    _cmd_todos(app, ["bogus"])
    assert "用法" in _output(app)


# ---------------------------------------------------------------- /changes


def test_changes_reports_empty_when_nothing_modified():
    app = _fake_policy_app()
    assert _cmd_changes(app, []) is False
    assert "还没有" in _output(app)


def test_changes_lists_modified_files_relative_to_workspace():
    app = _fake_policy_app()
    app.session.workspace.modified_files = {
        app.settings.workspace / "src" / "core.py",
        app.settings.workspace / "tests" / "test_core.py",
    }
    _cmd_changes(app, [])
    output = _output(app)
    assert "改动了 2 个文件" in output
    assert "src/core.py" in output
    assert "tests/test_core.py" in output


# ---------------------------------------------------------------- /skills


def _write_skill(root, name: str, description: str = "desc", body: str = "正文") -> None:
    skill_dir = root / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\n{body}\n",
        encoding="utf-8",
    )


def _fake_skills_app(workspace: Path, home: Path | None = None) -> SimpleNamespace:
    app = _fake_policy_app()
    app.settings.workspace = workspace
    app.runtime.prompt_stack = SimpleNamespace(home=home, skills_enabled=False)
    app._pending_skill_reminder = None
    return app


def test_skills_reports_empty_when_none_found(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    app = _fake_skills_app(workspace)
    assert _cmd_skills(app, []) is False
    assert "没有发现任何 skill" in _output(app)


def test_skills_lists_name_description_and_path(tmp_path):
    workspace = tmp_path / "ws"
    _write_skill(workspace / ".cyan" / "skills", "commit-message", "规范提交信息")
    app = _fake_skills_app(workspace)
    _cmd_skills(app, [])
    output = _output(app)
    assert "commit-message" in output
    assert "项目" in output
    assert "规范提交信息" in output
    assert "SKILL.md" in output


def test_skills_includes_personal_scope(tmp_path):
    home = tmp_path / "home"
    workspace = tmp_path / "ws"
    workspace.mkdir()
    _write_skill(home / "skills", "debugging-methodology", "遇到报错时用")
    app = _fake_skills_app(workspace, home=home)
    _cmd_skills(app, [])
    output = _output(app)
    assert "debugging-methodology" in output
    assert "个人" in output


def test_skills_listing_shows_enabled_status(tmp_path):
    workspace = tmp_path / "ws"
    _write_skill(workspace / ".cyan" / "skills", "commit-message")
    app = _fake_skills_app(workspace)
    _cmd_skills(app, [])
    assert "启用" in _output(app)


def test_skills_disable_writes_project_settings_and_updates_listing(tmp_path):
    workspace = tmp_path / "ws"
    _write_skill(workspace / ".cyan" / "skills", "commit-message")
    app = _fake_skills_app(workspace)

    _cmd_skills(app, ["disable", "commit-message"])
    assert "已禁用" in _output(app)
    assert (workspace / ".cyan" / "skills.json").is_file()

    app2 = _fake_skills_app(workspace)
    _cmd_skills(app2, [])
    assert "已禁用" in _output(app2)


def test_skills_enable_reverts_disabled_state(tmp_path):
    workspace = tmp_path / "ws"
    _write_skill(workspace / ".cyan" / "skills", "commit-message")
    app = _fake_skills_app(workspace)
    _cmd_skills(app, ["disable", "commit-message"])
    _cmd_skills(app, ["enable", "commit-message"])

    app2 = _fake_skills_app(workspace)
    _cmd_skills(app2, [])
    output = _output(app2)
    assert "已禁用" not in output


def test_skills_disable_unknown_name_reports_error(tmp_path):
    workspace = tmp_path / "ws"
    _write_skill(workspace / ".cyan" / "skills", "commit-message")
    app = _fake_skills_app(workspace)
    _cmd_skills(app, ["disable", "does-not-exist"])
    assert "未找到" in _output(app)


def test_skills_disable_missing_name_reports_usage(tmp_path):
    workspace = tmp_path / "ws"
    app = _fake_skills_app(workspace)
    _cmd_skills(app, ["disable"])
    assert "用法" in _output(app)


def test_skill_manual_invoke_warns_when_disabled(tmp_path):
    workspace = tmp_path / "ws"
    _write_skill(workspace / ".cyan" / "skills", "commit-message")
    app = _fake_skills_app(workspace)
    _cmd_skills(app, ["disable", "commit-message"])
    _cmd_skill(app, ["commit-message"])
    output = _output(app)
    assert app._pending_skill_reminder is not None
    assert "不会自动注入" in output


# ---------------------------------------------------------------- /skill（手动提醒）


def test_skill_no_args_lists_available_names():
    workspace_root = Path(__file__).resolve().parent.parent
    app = _fake_skills_app(workspace_root / "tests" / "fixtures-does-not-exist")
    assert _cmd_skill(app, []) is False
    assert "用法" in _output(app)


def test_skill_sets_pending_reminder_for_known_skill(tmp_path):
    workspace = tmp_path / "ws"
    _write_skill(workspace / ".cyan" / "skills", "commit-message", "规范提交信息", body="一次一件事")
    app = _fake_skills_app(workspace)
    _cmd_skill(app, ["commit-message"])
    assert app._pending_skill_reminder is not None
    assert "commit-message" in app._pending_skill_reminder
    assert "一次一件事" in app._pending_skill_reminder
    assert "已指定" in _output(app)


def test_skill_unknown_name_reports_available_choices(tmp_path):
    workspace = tmp_path / "ws"
    _write_skill(workspace / ".cyan" / "skills", "commit-message")
    app = _fake_skills_app(workspace)
    _cmd_skill(app, ["does-not-exist"])
    assert app._pending_skill_reminder is None
    assert "未找到" in _output(app)
    assert "commit-message" in _output(app)


def test_skill_clear_cancels_pending_reminder(tmp_path):
    workspace = tmp_path / "ws"
    _write_skill(workspace / ".cyan" / "skills", "commit-message")
    app = _fake_skills_app(workspace)
    _cmd_skill(app, ["commit-message"])
    assert app._pending_skill_reminder is not None
    _cmd_skill(app, ["clear"])
    assert app._pending_skill_reminder is None


# ---------------------------------------------------------------- /resume


class _FakeResumeApp:
    """够 ``_cmd_resume`` 用的最小假 app：真实 ``Session``/磁盘 store，
    ``attach_session`` 只做真实 ``App.attach_session`` 里跟本命令相关的那部分
    （换系统提示 + 换 self.session），不涉及 Runtime。
    """

    def __init__(self, workspace, session: Session) -> None:
        console = Console(file=io.StringIO(), force_terminal=True, width=80)
        self.settings = SimpleNamespace(workspace=workspace)
        self.renderer = Renderer(console)
        self.session = session

    def attach_session(self, session: Session) -> None:
        from cyan.core.prompts import build_system_prompt
        from cyan.session.view import apply_system_prompt

        apply_system_prompt(session, build_system_prompt(self.settings.workspace))
        self.session = session


def _make_disk_session(
    tmp_path, home, *, title: str | None = None, permission_mode: PermissionMode = PermissionMode.DEFAULT
) -> Session:
    from cyan.llm.types import UserMessage

    store = DiskStore.create(tmp_path, home=home)
    session = Session.create(
        workspace=tmp_path, system_prompt="sys", store=store, title=title, permission_mode=permission_mode
    )
    session.add(UserMessage.of("你好"))
    session.persist_head()
    return session


def test_resume_no_args_lists_sessions(tmp_path):
    home = tmp_path / "home"
    session_a = _make_disk_session(tmp_path, home, title="会话A")
    session_b = _make_disk_session(tmp_path, home, title="会话B")
    app = _FakeResumeApp(tmp_path, session_b)

    assert _cmd_resume(app, []) is False
    output = _output(app)
    assert session_a.metadata.session_id[:8] in output
    assert session_b.metadata.session_id[:8] in output


def test_resume_switches_to_target_session_by_prefix(tmp_path):
    home = tmp_path / "home"
    session_a = _make_disk_session(tmp_path, home, title="会话A")
    session_b = _make_disk_session(tmp_path, home, title="会话B")
    app = _FakeResumeApp(tmp_path, session_a)

    _cmd_resume(app, [session_b.metadata.session_id[:8]])

    assert app.session.metadata.session_id == session_b.metadata.session_id
    assert "会话B" in _output(app)


def test_resume_keeps_current_permission_mode_not_targets(tmp_path):
    home = tmp_path / "home"
    session_a = _make_disk_session(tmp_path, home, title="会话A", permission_mode=PermissionMode.ACCEPT_EDITS)
    session_b = _make_disk_session(tmp_path, home, title="会话B", permission_mode=PermissionMode.PLAN)
    app = _FakeResumeApp(tmp_path, session_a)

    _cmd_resume(app, [session_b.metadata.session_id[:8]])

    assert app.session.permissions.permission_mode == PermissionMode.ACCEPT_EDITS


def test_resume_unknown_token_reports_error_and_does_not_switch(tmp_path):
    home = tmp_path / "home"
    session_a = _make_disk_session(tmp_path, home, title="会话A")
    app = _FakeResumeApp(tmp_path, session_a)

    _cmd_resume(app, ["nope000"])

    assert "找不到会话" in _output(app)
    assert app.session is session_a


def test_resume_to_current_session_is_a_noop(tmp_path):
    home = tmp_path / "home"
    session_a = _make_disk_session(tmp_path, home, title="会话A")
    app = _FakeResumeApp(tmp_path, session_a)

    _cmd_resume(app, [session_a.metadata.session_id[:8]])

    assert "已经是当前会话" in _output(app)
    assert app.session is session_a
