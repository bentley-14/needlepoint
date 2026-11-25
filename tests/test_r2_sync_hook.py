"""CI-safe tests for the R2 artifact sync hook.

(a) orchestrate_pipeline._fire_r2_sync: completion hook fires detached Popen,
    sets manifest flag, respects NP_R2_DISABLE=1.
(b) np_launch.r2_configured: pure-function env/dotenv checks.
(c) sync_artifacts_r2.sh --check mode: skipped if bash or rclone absent.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import scripts.orchestrate_pipeline as op
from scripts.np_launch import r2_configured

REPO_ROOT = Path(__file__).resolve().parents[1]
SYNC_SCRIPT = REPO_ROOT / "scripts" / "sync_artifacts_r2.sh"


# ── (a) _fire_r2_sync: completion hook ───────────────────────────────────────

class TestFireR2Sync:
    def test_popen_called_detached(self, tmp_path, monkeypatch):
        """Hook spawns bash + sync_script with start_new_session=True."""
        monkeypatch.delenv("NP_R2_DISABLE", raising=False)

        # Ensure the sync script path exists (hook checks existence)
        fake_script = tmp_path / "sync_artifacts_r2.sh"
        fake_script.write_text("#!/usr/bin/env bash\nexit 0\n")
        fake_script.chmod(0o755)
        monkeypatch.setattr(op, "REPO_ROOT", tmp_path)
        (tmp_path / "scripts").mkdir()
        (tmp_path / "scripts" / "sync_artifacts_r2.sh").write_text("#!/usr/bin/env bash\nexit 0\n")
        (tmp_path / "scripts" / "sync_artifacts_r2.sh").chmod(0o755)

        run_dir = tmp_path / "state" / "runs" / "run_test"
        run_dir.mkdir(parents=True)
        manifest: dict = {}

        popen_calls = []

        class FakePopen:
            def __init__(self, cmd, **kwargs):
                popen_calls.append({"cmd": cmd, "kwargs": kwargs})
                self.pid = 12345

        with patch("subprocess.Popen", FakePopen):
            op._fire_r2_sync(run_dir, manifest)

        assert len(popen_calls) == 1
        call = popen_calls[0]
        # command must reference the sync script
        assert "bash" in call["cmd"]
        assert str(tmp_path / "scripts" / "sync_artifacts_r2.sh") in call["cmd"]
        # must be detached (start_new_session=True)
        assert call["kwargs"].get("start_new_session") is True
        # stdin must be DEVNULL (non-blocking)
        assert call["kwargs"].get("stdin") == subprocess.DEVNULL
        # manifest flag set
        assert manifest["r2_sync_started"] is True

    def test_np_r2_disable_suppresses(self, tmp_path, monkeypatch):
        """NP_R2_DISABLE=1 → r2_sync_started=False, no Popen."""
        monkeypatch.setenv("NP_R2_DISABLE", "1")
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        manifest: dict = {}

        popen_calls = []
        with patch("subprocess.Popen", side_effect=lambda *a, **k: popen_calls.append(1)):
            op._fire_r2_sync(run_dir, manifest)

        assert popen_calls == []
        assert manifest["r2_sync_started"] is False

    def test_missing_script_sets_false(self, tmp_path, monkeypatch):
        """If sync_artifacts_r2.sh is absent, manifest flag is False, no crash."""
        monkeypatch.delenv("NP_R2_DISABLE", raising=False)
        monkeypatch.setattr(op, "REPO_ROOT", tmp_path)
        # do NOT create scripts/sync_artifacts_r2.sh
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        manifest: dict = {}

        with patch("subprocess.Popen", side_effect=AssertionError("should not call Popen")):
            op._fire_r2_sync(run_dir, manifest)

        assert manifest["r2_sync_started"] is False

    def test_popen_exception_is_swallowed(self, tmp_path, monkeypatch):
        """A Popen failure (e.g., bash not found) must not propagate."""
        monkeypatch.delenv("NP_R2_DISABLE", raising=False)
        (tmp_path / "scripts").mkdir()
        (tmp_path / "scripts" / "sync_artifacts_r2.sh").write_text("#!/usr/bin/env bash\n")
        monkeypatch.setattr(op, "REPO_ROOT", tmp_path)
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        manifest: dict = {}

        with patch("subprocess.Popen", side_effect=OSError("bash not found")):
            op._fire_r2_sync(run_dir, manifest)  # must not raise

        assert manifest["r2_sync_started"] is False


# ── (b) r2_configured: pure-function checks ──────────────────────────────────

class TestR2Configured:
    def test_all_env_vars_present(self, tmp_path):
        env = {
            "R2_ACCOUNT_ID": "abc123",
            "R2_ACCESS_KEY_ID": "AKID",
            "R2_SECRET_ACCESS_KEY": "SECRET",
        }
        assert r2_configured(env=env, dotenv_path=tmp_path / ".env") is True

    def test_partial_env_vars_not_enough(self, tmp_path):
        env = {"R2_ACCOUNT_ID": "abc123", "R2_ACCESS_KEY_ID": "AKID"}
        # no .env file, rclone has no r2 remote
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="dropbox:\n")
            assert r2_configured(env=env, dotenv_path=tmp_path / ".env") is False

    def test_dotenv_present(self, tmp_path):
        dotenv = tmp_path / ".env"
        dotenv.write_text(
            "R2_ACCOUNT_ID=acct\nR2_ACCESS_KEY_ID=kid\nR2_SECRET_ACCESS_KEY=sec\n"
        )
        assert r2_configured(env={}, dotenv_path=dotenv) is True

    def test_dotenv_partial_not_enough(self, tmp_path):
        dotenv = tmp_path / ".env"
        dotenv.write_text("R2_ACCOUNT_ID=acct\nR2_ACCESS_KEY_ID=kid\n")
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="dropbox:\n")
            assert r2_configured(env={}, dotenv_path=dotenv) is False

    def test_absent_all(self, tmp_path):
        # No env vars, no .env, rclone will be mocked to return no r2 remote
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="dropbox:\n")
            assert r2_configured(env={}, dotenv_path=tmp_path / ".env") is False

    def test_rclone_remote_present(self, tmp_path):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="dropbox:\nr2:\n")
            assert r2_configured(env={}, dotenv_path=tmp_path / ".env") is True

    def test_rclone_exception_returns_false(self, tmp_path):
        with patch("subprocess.run", side_effect=OSError("no rclone")):
            assert r2_configured(env={}, dotenv_path=tmp_path / ".env") is False

    def test_dotenv_quoted_values(self, tmp_path):
        dotenv = tmp_path / ".env"
        dotenv.write_text(
            'R2_ACCOUNT_ID="acct"\nR2_ACCESS_KEY_ID=\'kid\'\nR2_SECRET_ACCESS_KEY=sec\n'
        )
        assert r2_configured(env={}, dotenv_path=dotenv) is True

    def test_env_wins_over_dotenv(self, tmp_path):
        """Env present → True even if dotenv is empty."""
        dotenv = tmp_path / ".env"
        dotenv.write_text("")
        env = {
            "R2_ACCOUNT_ID": "x",
            "R2_ACCESS_KEY_ID": "y",
            "R2_SECRET_ACCESS_KEY": "z",
        }
        assert r2_configured(env=env, dotenv_path=dotenv) is True


# ── (c) --check mode in sync_artifacts_r2.sh ─────────────────────────────────

def _bash_and_rclone_available() -> bool:
    return (
        bool(__import__("shutil").which("bash"))
        and bool(__import__("shutil").which("rclone"))
    )


@pytest.mark.skipif(
    not _bash_and_rclone_available(),
    reason="bash or rclone not available — skipping shell script check-mode test",
)
class TestSyncScriptCheckMode:
    def test_check_mode_exits_zero(self):
        """--check always exits 0 regardless of credential state."""
        result = subprocess.run(
            ["bash", str(SYNC_SCRIPT), "--check"],
            capture_output=True, text=True,
            env={**os.environ, "NP_R2_DISABLE": "0"},
        )
        assert result.returncode == 0

    def test_check_mode_prints_configured_or_unconfigured(self):
        """--check prints exactly 'configured' or 'unconfigured'."""
        result = subprocess.run(
            ["bash", str(SYNC_SCRIPT), "--check"],
            capture_output=True, text=True,
            env={**os.environ, "NP_R2_DISABLE": "0"},
        )
        assert result.stdout.strip() in ("configured", "unconfigured")

    def test_np_r2_disable_silences_check(self):
        """NP_R2_DISABLE=1 → silent exit 0 even in --check mode."""
        result = subprocess.run(
            ["bash", str(SYNC_SCRIPT), "--check"],
            capture_output=True, text=True,
            env={**os.environ, "NP_R2_DISABLE": "1"},
        )
        assert result.returncode == 0
        assert result.stdout.strip() == ""
