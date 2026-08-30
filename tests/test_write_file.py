"""write_file：新建、覆写与「先读后写」。"""

from __future__ import annotations


def test_creates_parent_directories(env, tmp_path):
    result = env.registry.execute("write_file", {"path": "sub/new.py", "content": "print('hi')\n"}, env.ctx)
    assert result.ok
    assert (tmp_path / "sub" / "new.py").is_file()
    assert "创建" in result.content


def test_new_file_does_not_require_prior_read(env, tmp_path):
    result = env.registry.execute("write_file", {"path": "brand_new.py", "content": "y = 1\n"}, env.ctx)
    assert result.ok
    assert (tmp_path / "brand_new.py").read_text(encoding="utf-8") == "y = 1\n"


def test_overwrite_without_read_is_rejected(env, tmp_path):
    (tmp_path / "guarded.py").write_text("x = 1\n", encoding="utf-8")
    result = env.registry.execute("write_file", {"path": "guarded.py", "content": "x = 2\n"}, env.ctx)
    assert not result.ok
    assert "还没有读取过" in (result.error or "")


def test_describe_unreads_existing_file(env, tmp_path):
    (tmp_path / "guarded.py").write_text("x = 1\n", encoding="utf-8")
    summary, detail, _fmt = env.registry.get("write_file").describe(
        {"path": "guarded.py", "content": "x = 2\n"},
        tmp_path,
        workspace_access=env.ctx.workspace_access,
    )
    assert "无法写入" in summary
    assert "还没有读取过" in (detail or "")


def test_overwrite_preserves_crlf(env, tmp_path):
    (tmp_path / "win.txt").write_bytes(b"old\r\n")
    env.registry.execute("read_file", {"path": "win.txt"}, env.ctx)
    result = env.registry.execute(
        "write_file", {"path": "win.txt", "content": "new\nline\n"}, env.ctx
    )
    assert result.ok
    assert (tmp_path / "win.txt").read_bytes() == b"new\r\nline\r\n"


def test_write_rejects_oversized_content(make_env, tmp_path):
    from cyan.settings import ToolLimits

    env = make_env(tools=ToolLimits(max_file_bytes=20))
    result = env.registry.execute(
        "write_file", {"path": "big.txt", "content": "x" * 50}, env.ctx
    )
    assert not result.ok
    assert "超过" in (result.error or "")


def test_overwrite_after_read(env, tmp_path):
    (tmp_path / "guarded.py").write_text("x = 1\n", encoding="utf-8")
    assert env.registry.execute("read_file", {"path": "guarded.py"}, env.ctx).ok
    result = env.registry.execute("write_file", {"path": "guarded.py", "content": "x = 2\n"}, env.ctx)
    assert result.ok
    assert (tmp_path / "guarded.py").read_text(encoding="utf-8") == "x = 2\n"
    assert "diff" in result.metadata
    assert "-x = 1" in result.metadata["diff"] or "x = 1" in result.metadata["diff"]


def test_describe_new_file_shows_diff(env, tmp_path):
    summary, detail, fmt = env.registry.get("write_file").describe(
        {"path": "fresh.py", "content": "print(1)\n"},
        tmp_path,
        workspace_access=env.ctx.workspace_access,
    )
    assert "新建" in summary
    assert fmt == "diff"
    assert "print(1)" in (detail or "")


def test_missing_content(env):
    result = env.registry.execute("write_file", {"path": "a.py"}, env.ctx)
    assert not result.ok
    assert "缺少必填参数" in (result.error or "")
