"""内置 shell 名单来自 defaults.json。"""

from __future__ import annotations

from cyan.security.catalog import ShellCatalog, shell_catalog
from cyan.security.shell import (
    is_accept_edits_fs_command,
    is_readonly_command,
    unwrap_argv,
)


def test_catalog_loads_builtin_lists():
    catalog = shell_catalog()
    assert "ls" in catalog.readonly_binaries
    assert "pytest" in catalog.readonly_binaries
    assert "status" in catalog.git_readonly_subcommands
    assert "mkdir" in catalog.accept_edits_fs
    assert "LANG" in catalog.safe_env_names
    assert catalog.unwrap["timeout"] == "timeout"
    assert catalog.unwrap["nohup"] == "passthrough"


def test_readonly_and_unwrap_use_catalog():
    assert is_readonly_command("ls")
    assert is_readonly_command("git status")
    assert unwrap_argv(["timeout", "30", "pytest", "-q"]).tokens == ["pytest", "-q"]
    assert unwrap_argv(["nohup", "ls"]).tokens == ["ls"]
    assert is_accept_edits_fs_command("mkdir tmp")


def test_readonly_list_comes_from_catalog(monkeypatch):
    real = shell_catalog()
    fake = ShellCatalog(
        readonly_binaries=frozenset({"customro"}),
        git_readonly_subcommands=real.git_readonly_subcommands,
        accept_edits_fs=real.accept_edits_fs,
        safe_env_names=real.safe_env_names,
        unwrap=real.unwrap,
    )
    monkeypatch.setattr("cyan.security.shell.shell_catalog", lambda: fake)
    assert is_readonly_command("customro")
    assert not is_readonly_command("ls")
