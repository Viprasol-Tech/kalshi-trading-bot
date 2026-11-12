"""Approved-host-only restic backup. Never echo credentials or provider errors."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

from kalshi_bot.hosting import backup
from kalshi_bot.safety import require_simulation


def main() -> None:
    require_simulation()
    if not all(
        Path("/etc/kalshi", marker).is_file()
        for marker in ("hosting-approved", "offsite-backup-approved")
    ):
        raise SystemExit("Separate hosting and off-site backup approvals required.")
    credentials = Path(os.environ["CREDENTIALS_DIRECTORY"])
    repository = (credentials / "restic-repository").read_text().strip()
    # Only credential-free, HTTPS R2 S3 repository URIs; no injected CLI arguments.
    if not re.fullmatch(
        r"s3:https://[a-f0-9]{32}\.r2\.cloudflarestorage\.com/[a-z0-9-]+/[a-z0-9/_-]+",
        repository,
    ):
        raise ValueError("Approved R2 repository URI required.")
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": "/var/backups/kalshi",
        "RESTIC_REPOSITORY": repository,
        "RESTIC_PASSWORD_FILE": str(credentials / "restic-password"),
        "AWS_ACCESS_KEY_ID": (credentials / "aws-access-key").read_text().strip(),
        "AWS_SECRET_ACCESS_KEY": (credentials / "aws-secret-key").read_text().strip(),
        "AWS_DEFAULT_REGION": "auto",
        "RESTIC_CACHE_DIR": "/var/backups/kalshi/cache",
    }
    root = Path(os.environ["KALSHI_BACKUPS"])
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    snapshot = root / ("set-" + str(time.time_ns()))
    backup(Path(os.environ["KALSHI_DATA"]), snapshot)
    # Stable relative paths allow restic to deduplicate SQLite pages across runs.
    # This command does NOT initialize/buy a bucket or repository.
    try:
        result = subprocess.run(
            [
                "/usr/bin/restic",
                "backup",
                "--quiet",
                "--tag",
                "kalshi-observation",
                "--host",
                "kalshi-vps",
                ".",
            ],
            cwd=snapshot,
            env=env,
            capture_output=True,
            timeout=180,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError("Encrypted off-site backup failed; provider output withheld.")
        # Only a successful remote upload advances monitoring's backup watermark.
        health = root / "last-success.json"
        temporary = root / ".last-success.pending"
        with temporary.open("w") as f:
            json.dump({"at": time.time(), "version": 1}, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary, health)
        print(json.dumps({"event": "offsite_backup_success", "at": time.time()}))
        # Remove only this consistent staging copy after confirmed upload.
        # Remote history is retained; pruning requires separate retention approval.
        shutil.rmtree(snapshot)
    except BaseException:
        # Keep failed local set for diagnosis. No infinite retries/spend.
        print('{"event":"offsite_backup_failed","data_retained":true}')
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
