"""Skills 发现与加载：cyan/prompt/skills.py。"""

from __future__ import annotations

from cyan.prompt.skills import (
    ENV_ENABLE_SKILLS,
    SKILL_FILENAME,
    discover_skills,
    load_skill_layers,
    project_skills_settings_path,
    set_skill_enabled,
    skill_settings_path,
    skills_layer_enabled,
    user_skills_settings_path,
)
from cyan.prompt.types import PromptLayerKind


def _write_skill(root, name: str, *, description: str = "desc", body: str = "正文内容") -> None:
    skill_dir = root / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / SKILL_FILENAME).write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\n{body}\n",
        encoding="utf-8",
    )


def test_discovers_personal_skill(tmp_path):
    home = tmp_path / "home"
    workspace = tmp_path / "ws"
    workspace.mkdir()
    _write_skill(home / "skills", "debugging-methodology", description="遇到报错时用")
    skills = discover_skills(workspace, home=home)
    assert len(skills) == 1
    meta = skills[0]
    assert meta.name == "debugging-methodology"
    assert meta.description == "遇到报错时用"
    assert meta.scope == "personal"
    assert meta.scope_label == "个人"
    assert meta.body == "正文内容"


def test_discovers_project_skill(tmp_path):
    workspace = tmp_path / "ws"
    _write_skill(workspace / ".cyan" / "skills", "release-checklist")
    skills = discover_skills(workspace, home=None)
    assert len(skills) == 1
    assert skills[0].scope == "project"
    assert skills[0].scope_label == "项目"


def test_home_none_skips_personal_layer(tmp_path):
    home = tmp_path / "home"
    workspace = tmp_path / "ws"
    _write_skill(home / "skills", "personal-only")
    skills = discover_skills(workspace, home=None)
    assert skills == []


def test_project_overrides_personal_on_name_collision(tmp_path):
    home = tmp_path / "home"
    workspace = tmp_path / "ws"
    _write_skill(home / "skills", "shared-name", body="个人版正文")
    _write_skill(workspace / ".cyan" / "skills", "shared-name", body="项目版正文")
    skills = discover_skills(workspace, home=home)
    assert len(skills) == 1
    assert skills[0].scope == "project"
    assert skills[0].body == "项目版正文"


def test_results_sorted_by_name(tmp_path):
    workspace = tmp_path / "ws"
    _write_skill(workspace / ".cyan" / "skills", "zeta")
    _write_skill(workspace / ".cyan" / "skills", "alpha")
    skills = discover_skills(workspace, home=None)
    assert [meta.name for meta in skills] == ["alpha", "zeta"]


def test_missing_skills_dir_returns_empty(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    assert discover_skills(workspace, home=tmp_path / "no-home") == []


def test_entry_without_skill_md_is_skipped(tmp_path):
    workspace = tmp_path / "ws"
    (workspace / ".cyan" / "skills" / "empty-dir").mkdir(parents=True)
    assert discover_skills(workspace, home=None) == []


def test_non_directory_entry_is_skipped(tmp_path):
    skills_dir = tmp_path / "ws" / ".cyan" / "skills"
    skills_dir.mkdir(parents=True)
    (skills_dir / "stray-file.md").write_text("not a skill dir", encoding="utf-8")
    assert discover_skills(tmp_path / "ws", home=None) == []


def test_missing_frontmatter_delimiter_is_skipped(tmp_path):
    skill_dir = tmp_path / "ws" / ".cyan" / "skills" / "broken"
    skill_dir.mkdir(parents=True)
    (skill_dir / SKILL_FILENAME).write_text("没有 frontmatter 的正文", encoding="utf-8")
    assert discover_skills(tmp_path / "ws", home=None) == []


def test_unclosed_frontmatter_is_skipped(tmp_path):
    skill_dir = tmp_path / "ws" / ".cyan" / "skills" / "unclosed"
    skill_dir.mkdir(parents=True)
    (skill_dir / SKILL_FILENAME).write_text(
        "---\nname: unclosed\ndescription: 缺少结尾分隔符\n\n正文",
        encoding="utf-8",
    )
    assert discover_skills(tmp_path / "ws", home=None) == []


def test_missing_required_field_is_skipped(tmp_path):
    skill_dir = tmp_path / "ws" / ".cyan" / "skills" / "no-description"
    skill_dir.mkdir(parents=True)
    (skill_dir / SKILL_FILENAME).write_text(
        "---\nname: no-description\n---\n\n正文",
        encoding="utf-8",
    )
    assert discover_skills(tmp_path / "ws", home=None) == []


def test_load_skill_layers_kind_and_title(tmp_path):
    workspace = tmp_path / "ws"
    _write_skill(
        workspace / ".cyan" / "skills",
        "writing-tests",
        description="补测试时用",
        body="覆盖正常/边界/错误路径",
    )
    layers = load_skill_layers(workspace, home=None)
    assert len(layers) == 1
    layer = layers[0]
    assert layer.kind is PromptLayerKind.SKILL
    assert layer.title == "Skill · writing-tests（项目）"
    assert "触发条件：补测试时用" in layer.text
    assert "覆盖正常/边界/错误路径" in layer.text
    assert layer.source == workspace / ".cyan" / "skills" / "writing-tests" / SKILL_FILENAME
    assert not layer.truncated


def test_load_skill_layers_truncates_long_body(tmp_path):
    workspace = tmp_path / "ws"
    _write_skill(workspace / ".cyan" / "skills", "huge", body="x" * 100)
    layers = load_skill_layers(workspace, home=None, max_chars=20)
    assert len(layers) == 1
    assert layers[0].truncated
    assert layers[0].text.endswith("...[truncated]")
    assert len(layers[0].text) == 20


# ---------------------------------------------------------------- enable/disable 开关


def test_all_skills_enabled_by_default(tmp_path):
    workspace = tmp_path / "ws"
    _write_skill(workspace / ".cyan" / "skills", "commit-message")
    skills = discover_skills(workspace, home=None)
    assert skills[0].enabled is True


def test_disable_project_skill_hides_it_from_layers_but_not_from_listing(tmp_path):
    workspace = tmp_path / "ws"
    _write_skill(workspace / ".cyan" / "skills", "commit-message")
    set_skill_enabled(project_skills_settings_path(workspace), "commit-message", False)

    skills = discover_skills(workspace, home=None)
    assert len(skills) == 1
    assert skills[0].enabled is False

    layers = load_skill_layers(workspace, home=None)
    assert layers == []


def test_disable_personal_skill_via_user_settings(tmp_path):
    home = tmp_path / "home"
    workspace = tmp_path / "ws"
    workspace.mkdir()
    _write_skill(home / "skills", "debugging-methodology")
    set_skill_enabled(user_skills_settings_path(home), "debugging-methodology", False)

    skills = discover_skills(workspace, home=home)
    assert skills[0].enabled is False
    assert load_skill_layers(workspace, home=home) == []


def test_re_enabling_clears_disabled_flag(tmp_path):
    workspace = tmp_path / "ws"
    _write_skill(workspace / ".cyan" / "skills", "commit-message")
    path = project_skills_settings_path(workspace)
    set_skill_enabled(path, "commit-message", False)
    assert discover_skills(workspace, home=None)[0].enabled is False

    set_skill_enabled(path, "commit-message", True)
    assert discover_skills(workspace, home=None)[0].enabled is True


def test_disabling_one_skill_does_not_affect_others(tmp_path):
    workspace = tmp_path / "ws"
    _write_skill(workspace / ".cyan" / "skills", "alpha")
    _write_skill(workspace / ".cyan" / "skills", "beta")
    set_skill_enabled(project_skills_settings_path(workspace), "alpha", False)

    skills = {meta.name: meta.enabled for meta in discover_skills(workspace, home=None)}
    assert skills == {"alpha": False, "beta": True}


def test_skill_settings_path_routes_by_scope(tmp_path):
    home = tmp_path / "home"
    workspace = tmp_path / "ws"
    assert skill_settings_path("personal", workspace, home=home) == home / "skills.json"
    assert skill_settings_path("project", workspace, home=home) == workspace / ".cyan" / "skills.json"
    assert skill_settings_path("personal", workspace, home=None) is None


def test_malformed_settings_file_is_treated_as_no_disabled(tmp_path):
    workspace = tmp_path / "ws"
    _write_skill(workspace / ".cyan" / "skills", "commit-message")
    project_skills_settings_path(workspace).parent.mkdir(parents=True, exist_ok=True)
    project_skills_settings_path(workspace).write_text("not json", encoding="utf-8")
    skills = discover_skills(workspace, home=None)
    assert skills[0].enabled is True


# ---------------------------------------------------------------- skills_layer_enabled


def test_skills_layer_disabled_by_default(monkeypatch):
    monkeypatch.delenv(ENV_ENABLE_SKILLS, raising=False)
    assert skills_layer_enabled() is False


def test_skills_layer_enabled_via_env(monkeypatch):
    monkeypatch.setenv(ENV_ENABLE_SKILLS, "1")
    assert skills_layer_enabled() is True


def test_skills_layer_env_value_is_case_insensitive(monkeypatch):
    monkeypatch.setenv(ENV_ENABLE_SKILLS, "TRUE")
    assert skills_layer_enabled() is True


def test_skills_layer_env_other_value_stays_disabled(monkeypatch):
    monkeypatch.setenv(ENV_ENABLE_SKILLS, "0")
    assert skills_layer_enabled() is False
