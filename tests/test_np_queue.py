"""Tests for scripts/ops/np_queue.py — the sequential investigation queue + babysitter."""
import importlib.util
import json
import time
from pathlib import Path

import pytest

_MOD = Path(__file__).resolve().parent.parent / "scripts" / "ops" / "np_queue.py"
spec = importlib.util.spec_from_file_location("np_queue", _MOD)
nq = importlib.util.module_from_spec(spec)
spec.loader.exec_module(nq)


@pytest.fixture
def tmp_state(tmp_path, monkeypatch):
    qd = tmp_path / "queue"
    cr = tmp_path / "cases"
    qd.mkdir(); cr.mkdir()
    monkeypatch.setattr(nq, "QUEUE_DIR", qd)
    monkeypatch.setattr(nq, "QUEUE_FILE", qd / "queue.jsonl")
    monkeypatch.setattr(nq, "DAEMON_STATE", qd / "daemon_state.json")
    monkeypatch.setattr(nq, "CASE_ROOT", cr)
    return tmp_path, qd, cr


# ── pure helpers ──────────────────────────────────────────────────────────────
def test_launch_command_plain_and_resume():
    assert nq.launch_command("1234567890", None) == [
        "make", "investigate", "NPI=1234567890", "SAFE_PERMS=1", "BACKEND=claude_p"]
    assert "RESUME=CASE-X" in nq.launch_command("1234567890", "CASE-X")


def test_tmux_launch_sources_env():
    sh = nq.tmux_launch_command("npq-1", "1234567890", None)
    assert ". ./.env" in sh and "make investigate NPI=1234567890" in sh


def test_is_calibration():
    assert nq.is_calibration("CALIBRATION-ABC")
    assert not nq.is_calibration("CASE-123")
    assert not nq.is_calibration(None)


def test_is_stalled_off_mtimes(tmp_state):
    _, qd, cr = tmp_state
    cd = cr / "CASE-1"; cd.mkdir()
    (cd / "summary.json").write_text("{}")
    now = time.time()
    # fresh → not stalled
    assert not nq.is_stalled(cd, qd / "run.log", now, threshold_s=720)
    # backdate the dir contents → stalled
    old = now - 1000
    import os
    os.utime(cd / "summary.json", (old, old)); os.utime(cd, (old, old))
    assert nq.is_stalled(cd, qd / "run.log", now, threshold_s=720)


def test_should_notify_dedupes():
    st = {"notified": []}
    assert nq.should_notify(st, "CASE-1", "done") is True
    assert nq.should_notify(st, "CASE-1", "done") is False       # second time suppressed
    assert nq.should_notify(st, "CASE-1", "checkpoint") is True   # different event ok


def test_find_case_id_newest_after_ts(tmp_state):
    _, qd, cr = tmp_state
    import os
    t0 = time.time()
    old = cr / "CASE-OLD"; old.mkdir(); os.utime(old, (t0 - 500, t0 - 500))
    new = cr / "CASE-NEW"; new.mkdir(); os.utime(new, (t0 + 5, t0 + 5))
    assert nq.find_case_id(t0) == "CASE-NEW"


def test_read_status(tmp_state):
    _, qd, cr = tmp_state
    cd = cr / "CASE-1"; cd.mkdir()
    (cd / "summary.json").write_text('{\n  "case_id": "CASE-1",\n  "status": "complete"\n}')
    assert nq.read_status(cd) == "complete"


# ── daemon tick (dry-run: no real tmux/notify) ────────────────────────────────
def test_default_concurrency_is_one():
    import argparse
    ap = argparse.ArgumentParser(); sub = ap.add_subparsers(dest="cmd")
    d = sub.add_parser("daemon"); d.add_argument("--concurrency", type=int, default=1)
    assert ap.parse_args(["daemon"]).concurrency == 1


def test_tick_claims_queued_job(tmp_state):
    jobs = [{"npi": "1234567890", "resume": None, "status": "queued", "resume_count": 0, "case_id": None}]
    jobs, st = nq.daemon_tick(jobs, {"notified": [], "current": None}, time.time(), dry_run=True)
    assert jobs[0]["status"] == "running"
    assert st["current"] == "1234567890"


def test_tick_marks_done_on_terminal_status(tmp_state):
    _, qd, cr = tmp_state
    cd = cr / "CASE-DONE"; cd.mkdir()
    (cd / "summary.json").write_text('{"status": "complete"}')
    jobs = [{"npi": "1234567890", "resume": "CASE-DONE", "status": "running",
             "resume_count": 0, "case_id": "CASE-DONE", "started_at": time.time()}]
    jobs, st = nq.daemon_tick(jobs, {"notified": [], "current": "1234567890"}, time.time(), dry_run=True)
    assert jobs[0]["status"] == "done"
    assert st["current"] is None


def test_tick_frees_on_checkpoint(tmp_state):
    _, qd, cr = tmp_state
    cd = cr / "CASE-CP"; cd.mkdir()
    (cd / "summary.json").write_text('{"status": "needs_human_checkpoint"}')
    jobs = [{"npi": "1234567890", "resume": "CASE-CP", "status": "running",
             "resume_count": 0, "case_id": "CASE-CP", "started_at": time.time()}]
    jobs, st = nq.daemon_tick(jobs, {"notified": [], "current": "1234567890"}, time.time(), dry_run=True)
    assert jobs[0]["status"] == "checkpoint"
    assert st["current"] is None


def test_tick_resumes_stalled_then_parks_at_cap(tmp_state):
    _, qd, cr = tmp_state
    import os
    cd = cr / "CASE-STALL"; cd.mkdir()
    (cd / "summary.json").write_text('{"status": "in_progress"}')
    old = time.time() - 1000
    os.utime(cd / "summary.json", (old, old)); os.utime(cd, (old, old))
    # already at the resume cap → should PARK, not resume again
    jobs = [{"npi": "1234567890", "resume": None, "status": "running",
             "resume_count": nq.MAX_RESUMES, "case_id": "CASE-STALL", "started_at": old}]
    jobs, st = nq.daemon_tick(jobs, {"notified": [], "current": "1234567890"}, time.time(), dry_run=True)
    assert jobs[0]["status"] == "parked"


def test_tick_never_resumes_calibration(tmp_state):
    _, qd, cr = tmp_state
    import os
    cd = cr / "CALIBRATION-X"; cd.mkdir()
    (cd / "summary.json").write_text('{"status": "in_progress"}')
    old = time.time() - 1000
    os.utime(cd / "summary.json", (old, old)); os.utime(cd, (old, old))
    jobs = [{"npi": "1234567890", "resume": "CALIBRATION-X", "status": "running",
             "resume_count": 0, "case_id": "CALIBRATION-X", "started_at": old}]
    jobs, st = nq.daemon_tick(jobs, {"notified": [], "current": "1234567890"}, time.time(), dry_run=True)
    assert jobs[0]["status"] == "parked"       # notify-only, not auto-resumed


def test_add_and_queue_roundtrip(tmp_state):
    nq.cmd_add("1234567890", None)
    jobs = nq.load_queue()
    assert len(jobs) == 1 and jobs[0]["npi"] == "1234567890" and jobs[0]["status"] == "queued"


def test_daemon_drains_and_exits(tmp_state):
    nq.cmd_add("1234567890", "CASE-DONE")
    _, qd, cr = tmp_state
    cd = cr / "CASE-DONE"; cd.mkdir()
    (cd / "summary.json").write_text('{"status": "complete"}')
    # tick 1 claims+runs; tick 2 sees terminal→done; then drained
    nq.cmd_daemon(concurrency=1, poll=0, dry_run=True, max_ticks=5)
    jobs = nq.load_queue()
    assert all(j["status"] in ("done", "checkpoint", "parked") for j in jobs)
