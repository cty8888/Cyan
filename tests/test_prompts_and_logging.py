"""system prompt 与日志落盘。"""

from __future__ import annotations

import sys

from cyan.core.prompts import build_system_prompt
from cyan.logutil import get_logger, setup_logging


def test_system_prompt_includes_python_executable(tmp_path):
    prompt = build_system_prompt(tmp_path)
    assert sys.executable in prompt
    assert "Cyan" in prompt
    assert "项目根目录" in prompt
    assert "不要带上" in prompt
    assert "不跟 bash 的 cd 走" in prompt
    assert "glob / grep" in prompt
    assert "不要用 bash 的 find、grep、rg 代替" in prompt


def test_logging_writes_file_not_stderr(env):
    log_path = setup_logging(env.settings.log_dir, level="INFO", to_stderr=False)
    get_logger("cli").info("smoke-marker-42")
    logged = log_path.read_text(encoding="utf-8")
    assert log_path.is_file()
    assert "smoke-marker-42" in logged
    assert sum(h.__class__.__name__ == "StreamHandler" for h in get_logger().handlers) == 0
