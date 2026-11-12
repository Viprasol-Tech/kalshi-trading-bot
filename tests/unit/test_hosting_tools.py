"""Optional genuine local tool drills: no services and no external endpoints."""

from __future__ import annotations

import os
import secrets
import shutil
import subprocess
from pathlib import Path

import pytest

from kalshi_bot.btc_observe import ledger_state
from kalshi_bot.hosting import backup, restore
from tests.unit.test_hosting import data_dir


def local_tool(name: str) -> str:
    path = shutil.which(name)
    if path is None:
        pytest.skip(f"Optional local {name} executable not installed.")
    return path


def test_real_restic_encrypted_local_repository_roundtrip(tmp_path: Path) -> None:
    restic = local_tool("restic")
    source = data_dir(tmp_path / "source")
    before = ledger_state(source / "paper.db")
    saved = tmp_path / "saved"
    backup(source, saved)
    password = tmp_path / "password"
    password.write_text(secrets.token_urlsafe(32))
    password.chmod(0o600)
    env = {
        **os.environ,
        "RESTIC_REPOSITORY": str(tmp_path / "encrypted-local-repo"),
        "RESTIC_PASSWORD_FILE": str(password),
        "RESTIC_CACHE_DIR": str(tmp_path / "cache"),
    }
    for command in (
        ["init"],
        ["backup", "--quiet", str(saved)],
        ["check", "--read-data"],
        ["restore", "latest", "--target", str(tmp_path / "download")],
    ):
        result = subprocess.run(
            [restic, *command], env=env, capture_output=True, timeout=60, check=False
        )
        assert result.returncode == 0, "Local encrypted restic drill failed; output withheld."
    extracted = tmp_path / "download" / str(saved).lstrip("/")
    clean = tmp_path / "clean"
    restore(extracted, clean)
    assert ledger_state(clean / "paper.db") == before == ledger_state(source / "paper.db")
    password.unlink()


def test_real_caddy_configuration_validation_without_starting_server(tmp_path: Path) -> None:
    caddy = local_tool("caddy")
    # Generate a throwaway password/hash on stdin, never command lines or output.
    hashed = subprocess.run(
        [caddy, "hash-password"],
        input=(secrets.token_urlsafe(32) + "\n").encode(),
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert hashed.returncode == 0, "Local fixture hashing failed; output withheld."
    config = Path(__file__).parents[2] / "deploy" / "digitalocean" / "Caddyfile"
    result = subprocess.run(
        [caddy, "validate", "--config", str(config), "--adapter", "caddyfile"],
        env={
            **os.environ,
            "DASHBOARD_USER": "offline-fixture",
            "DASHBOARD_PASSWORD_HASH": hashed.stdout.decode().strip(),
            "HOME": str(tmp_path),
        },
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, "Caddy validation failed; expanded config withheld."


def test_systemd_unit_syntax_in_synthetic_filesystem(tmp_path: Path) -> None:
    configured = os.environ.get("SYSTEMD_ANALYZE_BIN")
    analyze = configured if configured is not None else local_tool("systemd-analyze")
    root = tmp_path / "synthetic-host"
    units = root / "etc/systemd/system"
    units.mkdir(parents=True)
    source = Path(__file__).parents[2] / "deploy" / "digitalocean"
    copied = []
    for path in source.iterdir():
        if path.suffix in (".service", ".timer", ".slice"):
            target = units / path.name
            shutil.copyfile(path, target)
            copied.append("/etc/systemd/system/" + path.name)
    # Only verify unit syntax and dependencies. These inert placeholder paths
    # are NOT application executables or a deployment and are never run.
    for target in (
        "sysinit.target",
        "basic.target",
        "shutdown.target",
        "local-fs.target",
        "network-online.target",
        "multi-user.target",
        "timers.target",
        "slices.target",
        "time-set.target",
        "time-sync.target",
    ):
        (units / target).write_text("[Unit]\nDefaultDependencies=no\n")
    for name in ("opt/kalshi/.venv/bin/python", "opt/kalshi/.venv/bin/kalshi-bot", "usr/bin/caddy"):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("#!/bin/sh\nexit 99\n")
        path.chmod(0o755)
    result = subprocess.run(
        [analyze, "verify", "--man=no", "--root=" + str(root), *copied],
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr.decode()
