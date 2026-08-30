"""bash：执行命令、工作目录延续、超时与截断。"""

from __future__ import annotations

import sys

from cyan.settings import ToolLimits


def test_echo(env):
    result = env.registry.execute("bash", {"command": "echo hello"}, env.ctx)
    assert result.ok
    assert "hello" in result.content


def test_merges_stdout_and_stderr(env):
    result = env.registry.execute("bash", {"command": "echo out; echo err >&2"}, env.ctx)
    assert result.ok
    assert "out" in result.content
    assert "err" in result.content


def test_nonzero_exit_is_still_ok(env):
    """命令失败是给模型看的结果，不是工具没跑成。"""
    result = env.registry.execute("bash", {"command": "exit 3"}, env.ctx)
    assert result.ok
    assert "退出码：3" in result.content
    assert result.metadata.get("exit_code") == 3


def test_timeout(env):
    result = env.registry.execute("bash", {"command": "sleep 5", "timeout_ms": 200}, env.ctx)
    assert not result.ok
    assert "超时" in (result.error or "")


def test_can_run_project_python(env, tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "mod.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    (tmp_path / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    command = f'{sys.executable} -c "from pkg.mod import add; print(add(1, 2))"'
    result = env.registry.execute("bash", {"command": command}, env.ctx)
    assert result.ok
    assert "3" in result.content


def test_cwd_persists_across_calls(env, tmp_path):
    (tmp_path / "pkg").mkdir()
    result = env.registry.execute("bash", {"command": "cd pkg && pwd"}, env.ctx)
    assert result.ok
    assert str((tmp_path / "pkg").resolve()) in result.content
    result = env.registry.execute("bash", {"command": "pwd"}, env.ctx)
    assert result.ok
    assert str((tmp_path / "pkg").resolve()) in result.content


def test_leaving_workspace_is_rejected(env):
    result = env.registry.execute("bash", {"command": "cd / && pwd"}, env.ctx)
    assert not result.ok
    assert "之外" in (result.error or result.content or "")


def test_newline_cd_outside_then_write_is_rejected(env, tmp_path):
    result = env.registry.execute("bash", {"command": "cd /tmp\necho x > escaped.txt"}, env.ctx)
    assert not result.ok
    assert not (tmp_path / "escaped.txt").exists()


def test_opaque_cd_outside_still_resets_cwd(env, tmp_path):
    """``$HOME`` 解析不到真实路径，执行层拦不住；结束后仍把 cwd 拉回。"""
    result = env.registry.execute("bash", {"command": 'cd "$HOME" && pwd'}, env.ctx)
    assert result.ok
    assert "已重置回工作目录根" in result.content
    result = env.registry.execute("bash", {"command": "pwd"}, env.ctx)
    assert result.ok
    assert str(tmp_path.resolve()) in result.content


def test_redirect_outside_is_rejected(env):
    result = env.registry.execute("bash", {"command": "echo x > /tmp/cyan-escape.txt"}, env.ctx)
    assert not result.ok


def test_git_c_outside_is_rejected(env):
    result = env.registry.execute("bash", {"command": "git -C /tmp status"}, env.ctx)
    assert not result.ok
    assert "之外" in (result.error or result.content or "")


def test_env_c_outside_is_rejected(env):
    result = env.registry.execute("bash", {"command": "env -C /tmp cat .env"}, env.ctx)
    assert not result.ok
    assert "之外" in (result.error or result.content or "")


def test_write_git_dir_is_rejected(env, tmp_path):
    (tmp_path / ".git").mkdir(exist_ok=True)
    result = env.registry.execute("bash", {"command": "echo hacked > .git/config"}, env.ctx)
    assert not result.ok
    assert not (tmp_path / ".git" / "config").exists()


def test_sed_inplace_git_is_rejected(env, tmp_path):
    git = tmp_path / ".git"
    git.mkdir(exist_ok=True)
    (git / "config").write_text("old\n", encoding="utf-8")
    result = env.registry.execute("bash", {"command": "sed -i 's/old/new/' .git/config"}, env.ctx)
    assert not result.ok
    assert (git / "config").read_text(encoding="utf-8") == "old\n"


def test_output_truncation_keeps_head_and_tail(make_env, tmp_path):
    env = make_env(tools=ToolLimits(max_tool_output_chars=80))
    command = f'{sys.executable} -c "print(\'HEAD\'); print(\'x\' * 400); print(\'TAIL\')"'
    result = env.registry.execute("bash", {"command": command}, env.ctx)
    assert result.ok
    assert "HEAD" in result.content
    assert "TAIL" in result.content
    assert "...[truncated]" in result.content


def test_bash_write_unmarks_read_file(env, tmp_path):
    target = tmp_path / "a.py"
    target.write_text("old\n", encoding="utf-8")
    env.ctx.workspace_access.mark_read(target)
    result = env.registry.execute("bash", {"command": "echo new > a.py"}, env.ctx)
    assert result.ok
    assert not env.ctx.workspace_access.has_read(target)


def test_opaque_bash_clears_all_reads(env, tmp_path):
    target = tmp_path / "a.py"
    target.write_text("old\n", encoding="utf-8")
    env.ctx.workspace_access.mark_read(target)
    env.registry.execute("bash", {"command": "python rewrite.py"}, env.ctx)
    assert not env.ctx.workspace_access.has_read(target)


def test_env_does_not_persist(env):
    env.registry.execute("bash", {"command": "export CA_FLAG=1"}, env.ctx)
    result = env.registry.execute("bash", {"command": "echo ${CA_FLAG:-unset}"}, env.ctx)
    assert result.ok
    assert "unset" in result.content


def test_describe_shows_command(env, tmp_path):
    summary, detail, fmt = env.registry.get("bash").describe(
        {"command": "pytest -q"}, tmp_path
    )
    assert summary == "执行命令"
    assert detail == "pytest -q"
    assert fmt == "shell"


def test_blocked_command_at_execution_layer(env):
    result = env.registry.execute("bash", {"command": "sudo ls"}, env.ctx)
    assert not result.ok


def test_missing_command(env):
    result = env.registry.execute("bash", {}, env.ctx)
    assert not result.ok
    assert "缺少必填参数" in (result.error or "")
