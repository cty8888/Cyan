"""用户主目录下的会话路径：编码工作区、home、jsonl 与 sidecar。"""

from __future__ import annotations

import os
from pathlib import Path

ENV_HOME = "CYAN_HOME"
DEFAULT_HOME_NAME = ".cyan"


def cyan_home() -> Path:
    """会话存储根。``CYAN_HOME`` 覆盖，否则 ``~/.cyan``。"""
    raw = os.environ.get(ENV_HOME)
    if raw:
        return Path(raw).expanduser().resolve()
    return Path.home() / DEFAULT_HOME_NAME


def encode_workspace(workspace: Path) -> str:
    """把绝对路径编成目录名。

    先把 ``-`` 写成 ``--``，再把 ``:`` / ``/`` / ``\\`` 换成 ``-``，
    避免 ``/foo/bar`` 与 ``/foo-bar`` 编成同一个名字。
    """
    text = str(Path(workspace).expanduser().resolve())
    text = text.replace("-", "--")
    return text.replace(":", "-").replace("/", "-").replace("\\", "-")


def project_dir(workspace: Path, *, home: Path | None = None) -> Path:
    root = home if home is not None else cyan_home()
    return root / "projects" / encode_workspace(workspace)


def events_path(workspace: Path, session_id: str, *, home: Path | None = None) -> Path:
    return project_dir(workspace, home=home) / f"{session_id}.jsonl"


def sidecar_dir(workspace: Path, session_id: str, *, home: Path | None = None) -> Path:
    return project_dir(workspace, home=home) / session_id


def last_path(workspace: Path, *, home: Path | None = None) -> Path:
    return project_dir(workspace, home=home) / "last"


def settings_path(*, home: Path | None = None) -> Path:
    root = home if home is not None else cyan_home()
    return root / "settings.json"


def ensure_secure_dir(path: Path, *, mode: int = 0o700) -> None:
    path.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path, mode)
    except OSError:
        pass


def ensure_secure_file(path: Path, *, mode: int = 0o600) -> None:
    if not path.is_file():
        return
    try:
        os.chmod(path, mode)
    except OSError:
        pass
