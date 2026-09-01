"""Prompt Layer：cyan.md 组窗叠层，不进 Session / jsonl。"""

from __future__ import annotations

from cyan.context.builder import ContextBuilder
from cyan.context.types import ContextPolicy
from cyan.core.runtime import Runtime
from cyan.llm.types import SystemMessage
from cyan.prompt import PromptLayerKind, PromptStack, load_instruction_layers
from cyan.prompt.files import INSTRUCTION_FILENAME
from cyan.security.permissions import PermissionManager
from cyan.session import Session
from cyan.session.events import SESSION_STARTED
from cyan.session.store import DiskStore
from cyan.settings import AgentSettings, LLMSettings
from cyan.tools.registry import build_default_registry

from .conftest import FakeLLM, make_runtime


def _write_md(path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_load_user_then_project_order(tmp_path):
    home = tmp_path / "home"
    workspace = tmp_path / "ws"
    _write_md(home / INSTRUCTION_FILENAME, "user-rule")
    _write_md(workspace / INSTRUCTION_FILENAME, "project-rule")
    layers = load_instruction_layers(workspace, home=home)
    assert [layer.kind for layer in layers] == [
        PromptLayerKind.USER_INSTRUCTIONS,
        PromptLayerKind.PROJECT_INSTRUCTIONS,
    ]
    assert layers[0].text == "user-rule"
    assert layers[1].text == "project-rule"


def test_project_prefers_dot_cyan_over_root(tmp_path):
    workspace = tmp_path / "ws"
    _write_md(workspace / INSTRUCTION_FILENAME, "root-rule")
    _write_md(workspace / ".cyan" / INSTRUCTION_FILENAME, "nested-rule")
    layers = load_instruction_layers(workspace, home=None)
    assert len(layers) == 1
    assert layers[0].text == "nested-rule"


def test_project_falls_back_to_root_cyan_md(tmp_path):
    workspace = tmp_path / "ws"
    _write_md(workspace / INSTRUCTION_FILENAME, "root-rule")
    layers = load_instruction_layers(workspace, home=None)
    assert len(layers) == 1
    assert layers[0].text == "root-rule"


def test_missing_files_add_no_layers(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    workspace = tmp_path / "ws"
    workspace.mkdir()
    assert load_instruction_layers(workspace, home=home) == []


def test_empty_file_is_skipped(tmp_path):
    home = tmp_path / "home"
    workspace = tmp_path / "ws"
    _write_md(home / INSTRUCTION_FILENAME, "  \n")
    _write_md(workspace / INSTRUCTION_FILENAME, "ok")
    layers = load_instruction_layers(workspace, home=home)
    assert len(layers) == 1
    assert layers[0].kind is PromptLayerKind.PROJECT_INSTRUCTIONS


def test_home_none_skips_user_file(tmp_path):
    home = tmp_path / "home"
    workspace = tmp_path / "ws"
    _write_md(home / INSTRUCTION_FILENAME, "user-secret")
    _write_md(workspace / INSTRUCTION_FILENAME, "project-rule")
    layers = load_instruction_layers(workspace, home=None)
    assert len(layers) == 1
    assert layers[0].text == "project-rule"


def test_symlink_escape_is_skipped(tmp_path):
    home = tmp_path / "home"
    workspace = tmp_path / "ws"
    workspace.mkdir()
    secret = tmp_path / "secret.txt"
    secret.write_text("LEAK", encoding="utf-8")
    _write_md(home / INSTRUCTION_FILENAME, "user-ok")
    link = workspace / INSTRUCTION_FILENAME
    link.symlink_to(secret)
    layers = load_instruction_layers(workspace, home=home)
    assert [layer.kind for layer in layers] == [PromptLayerKind.USER_INSTRUCTIONS]
    assert all("LEAK" not in layer.text for layer in layers)


def test_layer_truncated_at_max_chars(tmp_path):
    workspace = tmp_path / "ws"
    _write_md(workspace / INSTRUCTION_FILENAME, "x" * 50)
    layers = load_instruction_layers(workspace, home=None, max_chars=20)
    assert len(layers) == 1
    assert layers[0].truncated
    assert layers[0].text.endswith("...[truncated]")
    assert len(layers[0].text) == 20


def test_render_passthrough_without_files(tmp_path):
    stack = PromptStack(workspace=tmp_path, home=tmp_path / "home")
    (tmp_path / "home").mkdir()
    assert stack.render_system("identity-text") == "identity-text"


def test_wire_stacks_layers_session_keeps_identity(tmp_path):
    home = tmp_path / "home"
    workspace = tmp_path / "ws"
    _write_md(home / INSTRUCTION_FILENAME, "prefer tabs")
    _write_md(workspace / INSTRUCTION_FILENAME, "run pytest")
    session = Session.create(workspace=workspace, system_prompt="identity-only")
    stack = PromptStack(workspace=workspace, home=home)
    payloads = ContextBuilder.from_policy(ContextPolicy()).build_messages(
        session.messages, session.tool_history, stack=stack
    )
    system = payloads[0]["content"]
    assert system.startswith("identity-only")
    assert "指令层 · 用户指令" in system
    assert "prefer tabs" in system
    assert "指令层 · 项目指令" in system
    assert "run pytest" in system
    assert isinstance(session.messages[0], SystemMessage)
    assert session.messages[0].text == "identity-only"


def test_session_started_jsonl_excludes_cyan_md(tmp_path):
    home = tmp_path / "home"
    workspace = tmp_path / "ws"
    _write_md(home / INSTRUCTION_FILENAME, "user-never-persist")
    _write_md(workspace / INSTRUCTION_FILENAME, "project-never-persist")
    store = DiskStore.create(workspace, home=home)
    session = Session.create(
        workspace=workspace, system_prompt="identity-only", store=store
    )
    assert "user-never-persist" not in (session.messages[0].text or "")
    raw = store.jsonl.read_text(encoding="utf-8")
    assert "user-never-persist" not in raw
    assert "project-never-persist" not in raw
    assert "identity-only" in raw
    started = [event for event in session.events if event.type == SESSION_STARTED]
    assert started
    assert "user-never-persist" not in str(started[0].payload)


def test_messages_for_request_rereads_disk(tmp_path):
    home = tmp_path / "home"
    workspace = tmp_path / "ws"
    _write_md(workspace / INSTRUCTION_FILENAME, "first")
    settings = AgentSettings(workspace=workspace, llm=LLMSettings(api_key="test"))
    session = Session.create(workspace=workspace, system_prompt="id")
    runtime = Runtime.create(
        settings,
        FakeLLM([]),
        build_default_registry(),
        PermissionManager(workspace),
        session,
        prompt_stack=PromptStack(workspace=workspace, home=home),
    )
    first = runtime.messages_for_request()[0]["content"]
    assert "first" in first
    _write_md(workspace / INSTRUCTION_FILENAME, "second")
    second = runtime.messages_for_request()[0]["content"]
    assert "second" in second
    assert "first" not in second
    assert session.messages[0].text == "id"


def test_wire_includes_skill_layer(tmp_path):
    home = tmp_path / "home"
    workspace = tmp_path / "ws"
    skill_dir = workspace / ".cyan" / "skills" / "commit-message"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: commit-message\ndescription: 创建 commit 时用\n---\n\n一个 commit 只做一件事",
        encoding="utf-8",
    )
    session = Session.create(workspace=workspace, system_prompt="identity-only")
    stack = PromptStack(workspace=workspace, home=home)
    payloads = ContextBuilder.from_policy(ContextPolicy()).build_messages(
        session.messages, session.tool_history, stack=stack
    )
    system = payloads[0]["content"]
    assert "Skill · commit-message（项目）" in system
    assert "触发条件：创建 commit 时用" in system
    assert "一个 commit 只做一件事" in system
    assert session.messages[0].text == "identity-only"


def test_wire_includes_memory_index_not_type_files(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    memory = workspace / ".cyan" / "memory"
    memory.mkdir(parents=True)
    (memory / "MEMORY.md").write_text("- [feedback] 不要用 find\n", encoding="utf-8")
    (memory / "feedback.md").write_text("TYPE_FILE_SECRET\n", encoding="utf-8")
    session = Session.create(workspace=workspace, system_prompt="identity-only")
    stack = PromptStack(workspace=workspace, auto_memory=True)
    payloads = ContextBuilder.from_policy(ContextPolicy()).build_messages(
        session.messages, session.tool_history, stack=stack
    )
    system = payloads[0]["content"]
    assert "不要用 find" in system
    assert "TYPE_FILE_SECRET" not in system
    assert session.messages[0].text == "identity-only"


def test_runtime_default_stack_does_not_read_user_home(tmp_path, make_env):
    home = tmp_path / "home"
    _write_md(home / INSTRUCTION_FILENAME, "user-secret")
    env = make_env()
    session = Session.create(workspace=env.settings.workspace, system_prompt="sys")
    runtime = make_runtime(env, FakeLLM([]), session)
    content = runtime.messages_for_request()[0]["content"]
    assert content == "sys"
    assert "user-secret" not in content
