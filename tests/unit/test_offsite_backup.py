"""Exercise the wrapper with a fake subprocess; never contact storage providers."""

from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from tests.unit.test_hosting import data_dir


def wrapper():
    path = Path(__file__).parents[2] / "deploy" / "digitalocean" / "offsite_backup.py"
    spec = importlib.util.spec_from_file_location("offline_backup_test_fixture", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_offsite_approval_refused_before_loading_credentials() -> None:
    module = wrapper()
    with (
        patch.object(Path, "is_file", return_value=False),
        pytest.raises(SystemExit, match="approval"),
    ):
        module.main()


@pytest.mark.parametrize("success", [True, False])
def test_remote_watermark_only_after_success_and_no_provider_output_leak(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, success: bool
) -> None:
    module = wrapper()
    credentials = tmp_path / "credentials"
    credentials.mkdir()
    # Pure fixtures, not credentials and never used in a network request.
    for name in ("restic-password", "aws-access-key", "aws-secret-key"):
        (credentials / name).write_text("noncredential-offline-test-fixture")
    (credentials / "restic-repository").write_text(
        "s3:https://" + "a" * 32 + ".r2.cloudflarestorage.com/test-bucket/test-prefix"
    )
    data = data_dir(tmp_path / "data")
    root = tmp_path / "backups"
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(credentials))
    monkeypatch.setenv("KALSHI_DATA", str(data))
    monkeypatch.setenv("KALSHI_BACKUPS", str(root))
    original_is_file = Path.is_file

    def approved_marker_or_real_file(path):
        if path.parent == Path("/etc/kalshi"):
            return True
        return original_is_file(path)

    response = subprocess.CompletedProcess(
        [], 0 if success else 1, b"PRIVATE_FIXTURE", b"PRIVATE_ERR"
    )
    with (
        patch.object(Path, "is_file", approved_marker_or_real_file),
        patch.object(module.subprocess, "run", return_value=response) as fake_run,
    ):
        if success:
            module.main()
            assert json.loads((root / "last-success.json").read_text())["version"] == 1
            assert not list(root.glob("set-*"))
        else:
            with pytest.raises(SystemExit):
                module.main()
            assert not (root / "last-success.json").exists()
            assert len(list(root.glob("set-*"))) == 1
        assert fake_run.call_count == 1
        command = fake_run.call_args.args[0]
        assert command[:2] == ["/usr/bin/restic", "backup"]
        assert "init" not in command and "forget" not in command and "prune" not in command
    captured = capsys.readouterr()
    assert "PRIVATE" not in captured.out + captured.err
    assert "noncredential" not in captured.out + captured.err
