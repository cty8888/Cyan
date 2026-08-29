"""工作区路径沙箱。"""

from __future__ import annotations

import pytest

from coding_agent.errors import PathOutsideWorkspaceError
from coding_agent.security.paths import resolve_path


def test_relative_escape_is_rejected(tmp_path):
    with pytest.raises(PathOutsideWorkspaceError):
        resolve_path(tmp_path, "../../etc/passwd")


def test_symlink_escape_is_rejected(tmp_path):
    (tmp_path / "link").symlink_to("/etc")
    with pytest.raises(PathOutsideWorkspaceError):
        resolve_path(tmp_path, "link/passwd")


def test_workspace_file_resolves(tmp_path):
    (tmp_path / "a.py").write_text("x\n", encoding="utf-8")
    resolved = resolve_path(tmp_path, "a.py")
    assert resolved == (tmp_path / "a.py").resolve()
