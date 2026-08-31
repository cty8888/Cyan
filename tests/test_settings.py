"""配置装配：环境变量、工作区 .env、命令行覆盖。"""

from __future__ import annotations


def test_workspace_dotenv_overrides_cwd(tmp_path, monkeypatch):
    cwd_dir = tmp_path / "cwd"
    workspace = tmp_path / "ws"
    cwd_dir.mkdir()
    workspace.mkdir()
    (cwd_dir / ".env").write_text("DEEPSEEK_API_KEY=from-cwd\n", encoding="utf-8")
    (workspace / ".env").write_text("DEEPSEEK_API_KEY=from-ws\n", encoding="utf-8")
    monkeypatch.chdir(cwd_dir)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)

    from cyan.settings.loader import load_settings

    settings = load_settings(workspace=workspace)
    assert settings.llm.api_key == "from-ws"
