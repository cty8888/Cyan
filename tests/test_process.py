"""子进程清理：超时或中断时不留孤儿。"""

from __future__ import annotations

import subprocess
import time

from cyan.tools.process import (
    build_subprocess_env,
    is_secret_env_name,
    run_process,
    terminate,
)


def test_api_key_env_names_are_secret():
    assert is_secret_env_name("DEEPSEEK_API_KEY")
    assert is_secret_env_name("OPENAI_API_KEY")
    assert is_secret_env_name("MY_CUSTOM_API_KEY")
    assert is_secret_env_name("SECRET_KEY")
    assert is_secret_env_name("DATABASE_URL")
    assert is_secret_env_name("MYSQL_PASSWORD")
    assert is_secret_env_name("APP_SECRET")
    assert not is_secret_env_name("PATH")
    assert not is_secret_env_name("HOME")


def test_subprocess_env_drops_api_key(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-secret-test")
    monkeypatch.setenv("CYAN_TEST_VISIBLE", "keep-me")
    env = build_subprocess_env()
    assert "DEEPSEEK_API_KEY" not in env
    assert env["CYAN_TEST_VISIBLE"] == "keep-me"


def test_subprocess_stdin_is_devnull(tmp_path):
    result = run_process(["bash", "-c", "cat; echo AFTER"], tmp_path, timeout=2, merge_stderr=True)
    assert result.timed_out is False
    assert "AFTER" in result.stdout


def test_subprocess_does_not_inherit_api_key(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-secret-test")
    result = run_process(
        ["bash", "-c", "printenv DEEPSEEK_API_KEY || true"],
        tmp_path,
        timeout=5,
    )
    assert "sk-secret-test" not in result.stdout


def test_output_cap_stops_before_oom(tmp_path):
    result = run_process(
        ["bash", "-c", "yes x | head -c 200000"],
        tmp_path,
        timeout=5,
        merge_stderr=True,
        max_output_chars=200,
    )
    assert result.output_capped is True
    assert len(result.stdout) <= 200


def test_timeout_does_not_leave_orphan(tmp_path):
    marker = "ca_timeout_marker_8821"
    result = run_process(f"sleep 60 # {marker}", tmp_path, timeout=0.2, shell=True)
    assert result.timed_out
    time.sleep(0.3)
    survivors = subprocess.run(["pgrep", "-f", marker], capture_output=True, text=True).stdout.split()
    subprocess.run(["pkill", "-f", marker], capture_output=True)
    assert not survivors, f"残留 {len(survivors)} 个进程"


def test_terminate_kills_process_group(tmp_path):
    marker = "ca_term_marker_8822"
    process = subprocess.Popen(
        ["bash", "-c", f"sleep 60 # {marker}"],
        cwd=str(tmp_path),
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    terminate(process)
    process.wait(timeout=5)
    time.sleep(0.2)
    survivors = subprocess.run(["pgrep", "-f", marker], capture_output=True, text=True).stdout.split()
    subprocess.run(["pkill", "-f", marker], capture_output=True)
    assert process.returncode is not None
    assert not survivors, f"残留 {len(survivors)} 个进程"
