"""发现并加载 Skills：个人级 + 项目级两层，全文直接进 system prompt（跟 cyan.md 同一套模式）。

跟 cyan.md 的关键区别是 cyan.md 全局只有一份，Skills 可以有很多个，各自独立触发。这里选
"整篇内容直接嵌进 system"，而不是"先给模型一句话摘要、需要时再让它调用 read_file 拉全文"：
项目级 skill 落在工作区沙箱内，``read_file`` 能读到；但个人级 skill 存在 ``~/.cyan/skills/``
下，天然越出 ``security/paths.py.resolve_path`` 只认工作区的沙箱，模型没法用现有工具读到它。
直接嵌入既避免了给沙箱开洞，也不用为此单独起一个工具，跟 cyan.md / MEMORY.md 已有的做法
（同样是全量嵌入、按 ``max_chars`` 截断）保持一致。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..settings.tools import DEFAULT_TOOL_RESULT_CHARS
from .files import truncate_text
from .types import PromptLayer, PromptLayerKind

SKILL_FILENAME = "SKILL.md"
_PERSONAL_SKILLS_DIRNAME = "skills"
_PROJECT_SKILLS_DIR = Path(".cyan") / "skills"
_FRONTMATTER_DELIM = "---"

# Skills 的常驻自动注入默认关闭：跟 cyan.md/MEMORY.md 不同，Skill 正文往往偏长、
# 数量也可能不少，默认全量塞进 system 对 token 开销不友好，所以改成显式 opt-in。
# ``CYAN_ENABLE_SKILLS=1`` 只决定**启动时**总开关是否打开；会话中途用 ``/skills on|off``
# 或 ``/skills enable <name>`` 即可立刻改，不必重启。发现（``/skills``）与手动指定
# 单次强调（``/skill <name>``）都不受启动默认值影响。
ENV_ENABLE_SKILLS = "CYAN_ENABLE_SKILLS"


def skills_layer_enabled() -> bool:
    """启动时是否打开 Skills 总开关；默认 False。会话中途由 ``PromptStack.skills_enabled`` 接管。"""
    raw = os.environ.get(ENV_ENABLE_SKILLS, "").strip().lower()
    return raw in {"1", "true", "yes", "on"}

# 开关状态是独立于 SKILL.md 本身的一份小配置：``{"disabled": ["name", ...]}``。跟
# cyan.md/权限规则一样按个人级/项目级两层存，互不覆盖——两边都能各自把某个 skill
# 关掉（并集），不像内容那样"同名项目级覆盖个人级"。
_SETTINGS_FILENAME = "skills.json"

SCOPE_PERSONAL = "personal"
SCOPE_PROJECT = "project"
_SCOPE_LABELS = {SCOPE_PERSONAL: "个人", SCOPE_PROJECT: "项目"}


@dataclass
class SkillMeta:
    """一个 Skill 的元信息 + 正文（frontmatter 之后的部分）。"""

    name: str
    description: str
    body: str
    path: Path
    scope: str  # SCOPE_PERSONAL | SCOPE_PROJECT
    enabled: bool = True

    @property
    def scope_label(self) -> str:
        return _SCOPE_LABELS.get(self.scope, self.scope)


def user_skills_settings_path(home: Path) -> Path:
    return Path(home) / _SETTINGS_FILENAME


def project_skills_settings_path(workspace: Path) -> Path:
    return Path(workspace) / ".cyan" / _SETTINGS_FILENAME


def skill_settings_path(scope: str, workspace: Path, *, home: Path | None = None) -> Path | None:
    """某个 skill 的开关该写进哪个文件：跟它自己的 scope 一致。

    个人级 skill 但没有 home（测试/隔离场景）时没地方写，返回 ``None``。
    """
    if scope == SCOPE_PERSONAL:
        return None if home is None else user_skills_settings_path(home)
    return project_skills_settings_path(workspace)


def set_skill_enabled(path: Path, name: str, enabled: bool) -> None:
    """在 ``path`` 指向的开关文件里启用/禁用 ``name``；文件不存在则新建。"""
    data = _read_settings(path)
    disabled = {str(item) for item in data.get("disabled") or []}
    if enabled:
        disabled.discard(name)
    else:
        disabled.add(name)
    data["disabled"] = sorted(disabled)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def discover_skills(workspace: Path, *, home: Path | None = None) -> list[SkillMeta]:
    """扫描个人级（``{home}/skills/``）与项目级（``{workspace}/.cyan/skills/``）两层。

    按 ``name`` 去重：同名时项目级覆盖个人级（跟 Claude Code 的 scope 优先级一致）。
    ``home is None`` 时不扫个人级，避免测试/无主目录场景误读开发者本机的 skills。
    结果按 name 排序，保证展示与测试的顺序稳定。每条附带 ``enabled``（个人级/项目级
    开关文件的并集里出现过 disabled 就算禁用），禁用的 skill 仍会被列出（供 ``/skills``
    展示状态），只在 :func:`load_skill_layers` 里被过滤掉，不会真正叠进 system prompt。
    """
    disabled: set[str] = set()
    if home is not None:
        disabled |= _load_disabled(user_skills_settings_path(home))
    disabled |= _load_disabled(project_skills_settings_path(workspace))

    found: dict[str, SkillMeta] = {}
    if home is not None:
        _collect(Path(home) / _PERSONAL_SKILLS_DIRNAME, SCOPE_PERSONAL, found)
    _collect(Path(workspace) / _PROJECT_SKILLS_DIR, SCOPE_PROJECT, found)
    for meta in found.values():
        meta.enabled = meta.name not in disabled
    return sorted(found.values(), key=lambda meta: meta.name)


def load_skill_layers(
    workspace: Path,
    *,
    home: Path | None = None,
    max_chars: int = DEFAULT_TOOL_RESULT_CHARS,
    only: set[str] | None = None,
) -> list[PromptLayer]:
    """把发现的 skill 转成可以直接塞进 ``PromptStack`` 的层。

    每层正文是「触发条件 + 完整正文」，超过 ``max_chars`` 按跟 cyan.md 相同的规则截断
    （截断标记算进上限）。被 ``/skills disable`` 关掉的 skill 不出现在返回结果里。
    ``only`` 非空时只加载这些名字（仍尊重 per-skill 禁用）；``None`` 表示所有启用中的。
    """
    layers: list[PromptLayer] = []
    for meta in discover_skills(workspace, home=home):
        if not meta.enabled:
            continue
        if only is not None and meta.name not in only:
            continue
        text = f"触发条件：{meta.description}\n\n{meta.body}"
        truncated = False
        if max_chars > 0 and len(text) > max_chars:
            text, truncated = truncate_text(text, max_chars)
        layers.append(
            PromptLayer(
                kind=PromptLayerKind.SKILL,
                title=f"Skill · {meta.name}（{meta.scope_label}）",
                text=text,
                source=meta.path,
                truncated=truncated,
            )
        )
    return layers


def _collect(root: Path, scope: str, found: dict[str, SkillMeta]) -> None:
    if not root.is_dir():
        return
    for entry in sorted(root.iterdir()):
        if not entry.is_dir():
            continue
        skill_file = entry / SKILL_FILENAME
        if not skill_file.is_file():
            continue
        try:
            text = skill_file.read_text(encoding="utf-8")
        except OSError:
            continue
        parsed = _parse_frontmatter(text)
        if parsed is None:
            continue
        fields, body = parsed
        name = fields.get("name", "").strip()
        description = fields.get("description", "").strip()
        if not name or not description:
            continue
        found[name] = SkillMeta(
            name=name,
            description=description,
            body=body.strip(),
            path=skill_file,
            scope=scope,
        )


def _read_settings(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _load_disabled(path: Path) -> set[str]:
    disabled = _read_settings(path).get("disabled")
    if not isinstance(disabled, list):
        return set()
    return {str(item) for item in disabled}


def _parse_frontmatter(text: str) -> tuple[dict[str, str], str] | None:
    """解析开头的 ``---\\nkey: value\\n---`` frontmatter；没头没尾/缺字段都返回 None。

    只需要 ``name`` / ``description`` 两个字段，手写十几行解析器足够，不必引入 PyYAML。
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != _FRONTMATTER_DELIM:
        return None
    fields: dict[str, str] = {}
    for index, line in enumerate(lines[1:], start=1):
        if line.strip() == _FRONTMATTER_DELIM:
            body = "\n".join(lines[index + 1 :])
            return fields, body
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        fields[key.strip()] = value.strip()
    return None
