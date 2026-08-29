"""子进程清理：超时或中断时不留孤儿。"""

from __future__ import annotations

import subprocess
import time

from coding_agent.tools.process import run_process, terminate


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
