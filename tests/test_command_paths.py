"""从 shell 命令抽出路径，供权限层按文件规则判定。"""

from __future__ import annotations

from coding_agent.security.command_paths import (
    analyze_command,
    forced_exec_reason,
    outside_workspace_reason,
    restricted_write_reason,
)
from coding_agent.security.messages import ENV_DUMP_MSG, OPAQUE_EXEC_MSG, UNBOUNDED_READ_MSG
from coding_agent.security.paths import resolve_path
from coding_agent.security.shell import command_head


def test_redirect_write_is_extracted():
    analysis = analyze_command("echo K=1 >> .env")
    assert any(touch.raw == ".env" and touch.kind == "write" for touch in analysis.touches)


def test_cat_read_is_extracted():
    analysis = analyze_command("cat src/a.py")
    assert any(touch.raw == "src/a.py" and touch.kind == "read" for touch in analysis.touches)


def test_python_c_is_opaque():
    analysis = analyze_command("python -c \"print(1)\"")
    assert analysis.opaque is True


def test_printenv_dumps_env():
    analysis = analyze_command("printenv")
    assert analysis.dumps_env is True


def test_env_wrapping_command_does_not_dump():
    analysis = analyze_command("env FOO=1 pytest -q")
    assert analysis.dumps_env is False


def test_outside_cat_is_denied(tmp_path):
    reason = outside_workspace_reason(tmp_path, "cat /etc/passwd")
    assert reason is not None
    assert "之外" in reason


def test_cd_outside_then_write(tmp_path):
    reason = outside_workspace_reason(tmp_path, "cd /tmp && echo x > a")
    assert reason is not None


def test_git_dir_redirect_is_restricted(tmp_path):
    reason = restricted_write_reason(tmp_path, "echo hacked > .git/config")
    assert reason is not None
    assert ".git" in reason


def test_env_file_write_is_forced(tmp_path):
    reason = forced_exec_reason(tmp_path, "echo K=1 >> .env")
    assert reason is not None
    assert ".env" in reason


def test_opaque_is_forced(tmp_path):
    assert forced_exec_reason(tmp_path, "python -c 'print(1)'") == OPAQUE_EXEC_MSG


def test_printenv_is_forced(tmp_path):
    assert forced_exec_reason(tmp_path, "printenv") == ENV_DUMP_MSG


def test_recursive_grep_is_unbounded(tmp_path):
    analysis = analyze_command("grep -rn SECRET .")
    assert analysis.unbounded_read is True
    assert forced_exec_reason(tmp_path, "grep -rn SECRET .") == UNBOUNDED_READ_MSG


def test_rg_is_unbounded(tmp_path):
    analysis = analyze_command("rg API_KEY src")
    assert analysis.unbounded_read is True
    assert forced_exec_reason(tmp_path, "rg API_KEY src") == UNBOUNDED_READ_MSG


def test_glob_cat_is_unbounded(tmp_path):
    analysis = analyze_command("cat *")
    assert analysis.unbounded_read is True
    assert forced_exec_reason(tmp_path, "cat *") == UNBOUNDED_READ_MSG


def test_quoted_star_is_not_unbounded():
    analysis = analyze_command("echo '2 * 3'")
    assert analysis.unbounded_read is False


def test_plain_grep_file_is_not_unbounded():
    analysis = analyze_command("grep foo src/a.py")
    assert analysis.unbounded_read is False


def test_command_head_includes_git_subcommand():
    assert command_head("git status") == "git status"
    assert command_head("git commit -m x") == "git commit"


def test_resolve_relative_to_base(tmp_path):
    nested = tmp_path / "pkg"
    nested.mkdir()
    (nested / "a.py").write_text("x\n", encoding="utf-8")
    resolved = resolve_path(tmp_path, "a.py", base=nested)
    assert resolved == (nested / "a.py").resolve()
