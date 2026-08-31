"""内置 shell 名单来自 defaults.json。"""

from __future__ import annotations

from dataclasses import replace

from cyan.security.catalog import shell_catalog
from cyan.security.command_paths import analyze_command
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
    assert "rm" in catalog.write_all
    assert "xargs" in catalog.opaque_heads
    assert "curl" in catalog.write_flag_next
    assert "-o" in catalog.write_flag_next["curl"]


def test_readonly_and_unwrap_use_catalog():
    assert is_readonly_command("ls")
    assert is_readonly_command("git status")
    assert unwrap_argv(["timeout", "30", "pytest", "-q"]).tokens == ["pytest", "-q"]
    assert unwrap_argv(["nohup", "ls"]).tokens == ["ls"]
    assert is_accept_edits_fs_command("mkdir tmp")
    assert analyze_command("cat a.txt").touches[0].raw == "a.txt"


def test_readonly_list_comes_from_catalog(monkeypatch):
    fake = replace(shell_catalog(), readonly_binaries=frozenset({"customro"}))
    monkeypatch.setattr("cyan.security.shell.shell_catalog", lambda: fake)
    assert is_readonly_command("customro")
    assert not is_readonly_command("ls")


def test_opaque_heads_come_from_catalog(monkeypatch):
    real = shell_catalog()
    fake = replace(real, opaque_heads=frozenset({"customopaque"}), interpreters=frozenset())
    monkeypatch.setattr("cyan.security.command_paths.shell_catalog", lambda: fake)
    assert analyze_command("customopaque foo").opaque
    assert not analyze_command("xargs echo").opaque
