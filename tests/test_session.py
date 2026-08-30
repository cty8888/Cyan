"""会话：重复调用指纹与进展重置。"""

from __future__ import annotations

from coding_agent.session import Session


def test_alternating_loop_is_detected(tmp_path):
    session = Session.create(workspace=tmp_path, system_prompt="")
    counts = []
    for _ in range(3):
        counts.append(session.record_call_fingerprint("read_file", {"path": "a.py"}))
        counts.append(session.record_call_fingerprint("read_file", {"path": "b.py"}))
    assert max(counts) >= 3


def test_reset_repeat_tracking_clears_counter(tmp_path):
    session = Session.create(workspace=tmp_path, system_prompt="")
    counts = []
    for _ in range(4):
        counts.append(session.record_call_fingerprint("bash", {"command": "pytest"}))
        session.record_call_fingerprint("edit_file", {"path": "x.py"})
        session.reset_repeat_tracking()
    assert max(counts) == 1
