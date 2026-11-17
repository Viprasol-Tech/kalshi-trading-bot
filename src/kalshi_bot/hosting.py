"""Offline hosting tools: consistent backups, no-overwrite restore, idle guard.

No provider SDKs, exchange calls, broker or collector. The guard is intentionally
NOT a campaign supervisor. Its heartbeat must never count as collection progress.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import shutil
import sqlite3
import time
import uuid
from contextlib import closing
from pathlib import Path
from typing import Any

from kalshi_bot.btc_dashboard import connect_readonly, snapshot
from kalshi_bot.safety import require_simulation

DB_NAMES = ("paper.db", "btc-auto-control.db", "observations.db", "campaign.db", "multi-paper.db")


def sync_dir(path: Path) -> None:
    if os.name != "nt":
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def backup(source: Path, destination: Path) -> dict[str, Any]:
    """SQLite online backup API includes committed WAL; publish one complete set.

    Per-database consistency, NOT a distributed multi-database transaction.
    A future worker must reconcile cross-database journals after any restore.
    Does not prune anything or include credentials/logs/environment files.
    """
    require_simulation()
    if destination.exists() or destination.is_symlink():
        raise ValueError("Backup destination must be new.")
    destination.parent.mkdir(parents=True, exist_ok=True)
    work = destination.parent / (".pending-" + uuid.uuid4().hex)
    work.mkdir(mode=0o700)
    manifest: dict[str, Any] = {
        "version": 1,
        "created_at": time.time(),
        "files": {},
        "scope": "per-database-consistent",
    }
    try:
        for name in DB_NAMES:
            original = source / name
            if not original.exists():
                continue
            if original.is_symlink() or not original.is_file():
                raise ValueError("Unexpected database path.")
            target = work / name
            with (
                closing(connect_readonly(original)) as src,
                closing(sqlite3.connect(target)) as dst,
            ):
                src.backup(dst)
                if dst.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                    raise ValueError("Backup integrity failed.")
            os.chmod(target, 0o600)
            with target.open("r+b") as f:
                os.fsync(f.fileno())
            manifest["files"][name] = {"sha256": digest(target), "bytes": target.stat().st_size}
        if not manifest["files"] or "paper.db" not in manifest["files"]:
            raise ValueError("Existing paper ledger required; never initialize one implicitly.")
        with (work / "manifest.json").open("x") as f:
            json.dump(manifest, f, sort_keys=True, allow_nan=False)
            f.flush()
            os.fsync(f.fileno())
        sync_dir(work)
        # A different invocation must never replace a completed destination.
        if destination.exists():
            raise ValueError("Backup destination already exists.")
        work.rename(destination)
        sync_dir(destination.parent)
        return manifest
    except BaseException:
        if work.exists():
            shutil.rmtree(work)
        raise


def verify_backup(source: Path) -> dict[str, Any]:
    manifest = json.loads((source / "manifest.json").read_text())
    if (
        manifest.get("version") != 1
        or not manifest.get("files")
        or "paper.db" not in manifest["files"]
        or set(manifest["files"]) - set(DB_NAMES)
        or source.is_symlink()
    ):
        raise ValueError("Invalid backup manifest.")
    for name, info in manifest["files"].items():
        path = source / name
        if (
            path.is_symlink()
            or not path.is_file()
            or digest(path) != info["sha256"]
            or path.stat().st_size != info["bytes"]
        ):
            raise ValueError("Backup checksum failed.")
        with closing(connect_readonly(path)) as db:
            if [r[0] for r in db.execute("PRAGMA integrity_check")] != ["ok"]:
                raise ValueError("Backup SQLite integrity failed.")
    return dict(manifest)


def restore(source: Path, destination: Path) -> dict[str, Any]:
    """Clean-directory-only restore. No running service, ledger overwrite or reset."""
    require_simulation()
    manifest = verify_backup(source)
    if destination.exists() or destination.is_symlink():
        raise ValueError("Restore destination must not exist.")
    destination.mkdir(mode=0o700, parents=True)
    try:
        for name in manifest["files"]:
            shutil.copyfile(source / name, destination / name)
            os.chmod(destination / name, 0o600)
            with (destination / name).open("r+b") as f:
                os.fsync(f.fileno())
        # Preserve manifest for verification/audit. This is not restart permission.
        shutil.copyfile(source / "manifest.json", destination / "manifest.json")
        verify_backup(destination)
        sync_dir(destination)
        sync_dir(destination.parent)
    except BaseException:
        shutil.rmtree(destination)
        raise
    return manifest


def guard_status(data: Path, *, now: float) -> dict[str, Any]:
    require_simulation()
    if not math.isfinite(now) or now <= 0:
        raise ValueError("Invalid health clock.")
    status: dict[str, Any] = {
        "at": now,
        "service": "idle-safety-guard",
        "collection_running": False,
        "entries_enabled": False,
        "live_enabled": False,
        "kill_armed": True,
        "kill_reason": "control unavailable; fail closed",
        "storage_ok": False,
        "errors": [],
    }
    try:
        with closing(connect_readonly(data / "btc-auto-control.db")) as db:
            row = db.execute("SELECT killed,reason FROM auto_control WHERE id=1").fetchone()
            if row is None:
                raise ValueError("Missing control.")
            status["kill_armed"], status["kill_reason"] = bool(row[0]), row[1]
            if not status["kill_armed"]:
                status["errors"].append("kill_latch_unexpectedly_disarmed")
        usage = shutil.disk_usage(data)
        status["free_bytes"] = usage.free
        status["storage_ok"] = usage.free >= 2 * 1024**3 and usage.free / usage.total >= 0.15
        if not status["storage_ok"]:
            status["errors"].append("disk_space_low")
    except (OSError, ValueError, sqlite3.Error):
        status["errors"].append("control_or_storage_unavailable")
    return status


def monitoring_status(data: Path, backups: Path, *, now: float) -> dict[str, Any]:
    """Idle guard and backup freshness, NOT market-feed quality or uptime proof."""
    result = guard_status(data, now=now)
    for name, path, maximum_age in (
        ("guard_heartbeat", data / "guard-health.json", 45),
        ("offsite_backup", backups / "last-success.json", 900),
    ):
        try:
            at = json.loads(path.read_text())["at"]
            if isinstance(at, bool) or not isinstance(at, (float, int)):
                raise ValueError("Invalid timestamp.")
            fresh = math.isfinite(at) and 0 <= now - at <= maximum_age
        except (OSError, ValueError, KeyError, TypeError):
            fresh = False
        result[name + "_fresh"] = fresh
        if not fresh:
            result["errors"].append(name + "_stale_or_missing")
    return result


def write_health(data: Path) -> None:
    """Atomic status only; never creates/refunds an account or changes control."""
    status = guard_status(data, now=time.time())
    target = data / "guard-health.json"
    temp = data / (".health-" + uuid.uuid4().hex)
    try:
        with temp.open("x") as f:
            os.chmod(temp, 0o640)
            json.dump(status, f, allow_nan=False, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp, target)
        sync_dir(data)
    finally:
        temp.unlink(missing_ok=True)


def publish_dashboard(data: Path) -> None:
    """Writer-side consistent reads; dashboard identity only serves this JSON.

    SQLite may need transient WAL/SHM files even for read-only queries. Keeping
    SQLite access here avoids granting filesystem write permission to the viewer.
    """
    payload = snapshot(
        data / "paper.db",
        journal=data / "btc-auto-control.db",
        observations=data / "observations.db" if (data / "observations.db").exists() else None,
    )
    temp = data / (".dashboard-" + uuid.uuid4().hex)
    try:
        with temp.open("x") as f:
            os.chmod(temp, 0o640)
            json.dump(payload, f, sort_keys=True, allow_nan=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp, data / "dashboard-published.json")
        sync_dir(data)
    finally:
        temp.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command", choices=("backup", "restore", "verify", "guard", "health", "monitor")
    )
    parser.add_argument("--data", type=Path, default=Path("/var/lib/kalshi"))
    parser.add_argument("--target", type=Path)
    parser.add_argument("--backups", type=Path, default=Path("/var/backups/kalshi"))
    args = parser.parse_args()
    require_simulation()
    if args.command in ("backup", "restore"):
        if args.target is None:
            parser.error("--target required")
        if args.command == "backup":
            backup(args.data, args.target)
        else:
            restore(args.data, args.target)
    elif args.command == "verify":
        verify_backup(args.data)
    elif args.command in ("health", "monitor"):
        status = (
            monitoring_status(args.data, args.backups, now=time.time())
            if args.command == "monitor"
            else guard_status(args.data, now=time.time())
        )
        print(json.dumps({k: v for k, v in status.items() if k != "kill_reason"}, sort_keys=True))
        if status["errors"]:
            raise SystemExit(1)
    else:
        while True:
            write_health(args.data)
            publish_dashboard(args.data)
            # Never log environment, exceptions, keys, headers or arbitrary reasons.
            print('{"event":"idle_guard_heartbeat","collection_running":false}', flush=True)
            time.sleep(15)


if __name__ == "__main__":
    main()
