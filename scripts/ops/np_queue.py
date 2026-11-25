#!/usr/bin/env python3
"""np_queue — sequential investigation queue + babysitter.

Runs production NeedlePoint investigations one at a time (concurrency 1 by
design: parallel runs drain the shared 5-hour subscription token window faster,
they don't add throughput). Its value is *unattended* operation:

  • launches each queued NPI via `make investigate ... BACKEND=claude_p` in its
    own tmux session, with the environment sourced from .env so
    CLAUDE_CODE_OAUTH_TOKEN reaches the spawned `claude -p` subprocesses;
  • auto-resumes a run that STALLS (case dir + log both quiet past a threshold
    while its tmux session is still alive), up to a cap, then parks + notifies;
  • fires a macOS notification (once per event) when a run hits a human
    CHECKPOINT or finishes, then advances the queue.

Calibration cases (CASE-ID starting "CALIBRATION-") are never auto-resumed —
they are notify-only, since re-running them can perturb a leak-safety measurement.

State lives in state/queue/{queue.jsonl, daemon_state.json} (atomic writes).

CLI:
    python scripts/ops/np_queue.py add --npi 1234567890 [--resume CASE-ID]
    python scripts/ops/np_queue.py list
    python scripts/ops/np_queue.py status
    python scripts/ops/np_queue.py daemon [--concurrency 1] [--poll 120]
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent.parent                       # repo root
QUEUE_DIR = _ROOT / "state" / "queue"
QUEUE_FILE = QUEUE_DIR / "queue.jsonl"
DAEMON_STATE = QUEUE_DIR / "daemon_state.json"
CASE_ROOT = _ROOT / "fraud-investigator-v2" / "state"

STALL_SECONDS = int(os.environ.get("NP_QUEUE_STALL_S", "720"))   # 12 min quiet == stalled
MAX_RESUMES = 2                                                  # auto-resumes before parking
TERMINAL_STATUSES = {"complete", "abandoned"}
CHECKPOINT_STATUS = "needs_human_checkpoint"


# ── atomic json/jsonl io ──────────────────────────────────────────────────────
def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def load_queue() -> list[dict]:
    if not QUEUE_FILE.exists():
        return []
    return [json.loads(l) for l in QUEUE_FILE.read_text().splitlines() if l.strip()]


def save_queue(jobs: list[dict]) -> None:
    _atomic_write(QUEUE_FILE, "\n".join(json.dumps(j) for j in jobs) + ("\n" if jobs else ""))


def load_daemon_state() -> dict:
    if not DAEMON_STATE.exists():
        return {"notified": [], "current": None}
    try:
        return json.loads(DAEMON_STATE.read_text())
    except Exception:
        return {"notified": [], "current": None}


def save_daemon_state(st: dict) -> None:
    _atomic_write(DAEMON_STATE, json.dumps(st, indent=1))


# ── pure helpers (unit-tested) ────────────────────────────────────────────────
def launch_command(npi: str, resume: str | None) -> list[str]:
    """The `make investigate` argv for a job. Env (.env) is sourced by the caller."""
    cmd = ["make", "investigate", f"NPI={npi}", "SAFE_PERMS=1", "BACKEND=claude_p"]
    if resume:
        cmd.append(f"RESUME={resume}")
    return cmd


def tmux_launch_command(session: str, npi: str, resume: str | None) -> str:
    """Full shell string tmux runs: source .env, then make investigate, tee a log."""
    inv = " ".join(launch_command(npi, resume))
    log = f"state/queue/run_{npi}.log"
    return (f'cd "{_ROOT}" && set -a; . ./.env; set +a; '
            f'{inv} 2>&1 | tee -a {log}')


def is_calibration(case_id: str | None) -> bool:
    return bool(case_id) and str(case_id).startswith("CALIBRATION-")


def dir_last_activity(case_dir: Path, log_path: Path) -> float:
    """Newest mtime across the case dir tree + the run log. 0.0 if nothing exists."""
    newest = 0.0
    for p in (case_dir, log_path):
        if p and p.exists():
            if p.is_dir():
                for child in p.rglob("*"):
                    try:
                        newest = max(newest, child.stat().st_mtime)
                    except OSError:
                        pass
                try:
                    newest = max(newest, p.stat().st_mtime)
                except OSError:
                    pass
            else:
                try:
                    newest = max(newest, p.stat().st_mtime)
                except OSError:
                    pass
    return newest


def is_stalled(case_dir: Path, log_path: Path, now: float, threshold_s: int = STALL_SECONDS) -> bool:
    last = dir_last_activity(case_dir, log_path)
    return last > 0.0 and (now - last) > threshold_s


def should_notify(daemon_state: dict, case_id: str, event: str) -> bool:
    """True the first time (case_id, event) is seen; records it so it won't repeat."""
    key = f"{case_id}:{event}"
    if key in daemon_state.get("notified", []):
        return False
    daemon_state.setdefault("notified", []).append(key)
    return True


def read_status(case_dir: Path) -> str | None:
    sj = case_dir / "summary.json"
    if not sj.exists():
        return None
    try:
        # summary.json can be large; scan for the status line cheaply
        for line in sj.read_text(errors="ignore").splitlines():
            if '"status"' in line:
                import re
                m = re.search(r'"status"\s*:\s*"([a-z_]+)"', line)
                if m:
                    return m.group(1)
    except Exception:
        return None
    return None


def find_case_id(since_ts: float) -> str | None:
    """Newest CASE-* dir created at/after since_ts (the run the daemon just launched)."""
    if not CASE_ROOT.exists():
        return None
    best, best_mt = None, since_ts - 1
    for d in CASE_ROOT.glob("CASE-*"):
        try:
            mt = d.stat().st_mtime
        except OSError:
            continue
        if mt >= since_ts and mt > best_mt:
            best, best_mt = d.name, mt
    return best


# ── side-effecting ops (faked under dry_run) ──────────────────────────────────
def notify(title: str, message: str, dry_run: bool = False) -> None:
    if dry_run:
        print(f"[notify] {title}: {message}")
        return
    try:
        subprocess.run(
            ["osascript", "-e", f'display notification "{message}" with title "{title}"'],
            check=False, capture_output=True, timeout=10)
    except Exception:
        pass
    print(f"[notify] {title}: {message}")


def tmux_alive(session: str, dry_run: bool = False) -> bool:
    if dry_run:
        return True
    r = subprocess.run(["tmux", "has-session", "-t", session],
                       capture_output=True, timeout=10)
    return r.returncode == 0


def tmux_kill(session: str, dry_run: bool = False) -> None:
    if dry_run:
        print(f"[tmux] would kill {session}")
        return
    subprocess.run(["tmux", "kill-session", "-t", session], capture_output=True, timeout=10)


def tmux_start(session: str, npi: str, resume: str | None, dry_run: bool = False) -> None:
    shell = tmux_launch_command(session, npi, resume)
    if dry_run:
        print(f"[tmux] would start {session}: {shell}")
        return
    subprocess.run(["tmux", "kill-session", "-t", session], capture_output=True, timeout=10)
    subprocess.run(["tmux", "new-session", "-d", "-s", session, shell],
                   capture_output=True, timeout=15)


# ── subcommands ───────────────────────────────────────────────────────────────
def cmd_add(npi: str, resume: str | None) -> None:
    if not (len(npi) == 10 and npi.isdigit()):
        sys.exit(f"ERROR: --npi must be a 10-digit NPI; got {npi!r}")
    jobs = load_queue()
    jobs.append({"npi": npi, "resume": resume, "status": "queued",
                 "added_at": time.time(), "case_id": None, "resume_count": 0})
    save_queue(jobs)
    print(f"queued NPI {npi}" + (f" (resume {resume})" if resume else "") + f" — {len(jobs)} in queue")


def cmd_list() -> None:
    jobs = load_queue()
    if not jobs:
        print("(queue empty)")
        return
    for i, j in enumerate(jobs):
        print(f"  {i:2} [{j['status']:8}] NPI {j['npi']}  case={j.get('case_id') or '-'}  "
              f"resumes={j.get('resume_count', 0)}")


def cmd_status() -> None:
    st = load_daemon_state()
    cur = st.get("current")
    print(f"current: {cur or '(idle)'}")
    print(f"notifications sent: {len(st.get('notified', []))}")
    cmd_list()


def _log_path(npi: str) -> Path:
    return QUEUE_DIR / f"run_{npi}.log"


def daemon_tick(jobs: list[dict], daemon_state: dict, now: float, dry_run: bool = False) -> tuple[list[dict], dict]:
    """One monitor/advance step. Pure-ish: all side effects go through the faked ops.
    Returns (updated jobs, updated daemon_state). Unit-tested via dry_run."""
    running = [j for j in jobs if j["status"] == "running"]

    # 1. monitor a running job
    for j in running:
        session = f"npq-{j['npi']}"
        # discover case id if not yet known
        if not j.get("case_id"):
            j["case_id"] = j.get("resume") or find_case_id(j.get("started_at", now))
        case_id = j.get("case_id")
        case_dir = CASE_ROOT / case_id if case_id else None
        status = read_status(case_dir) if case_dir else None

        if status in TERMINAL_STATUSES:
            if should_notify(daemon_state, case_id, "done"):
                notify("NeedlePoint", f"{j['npi']} {status} ({case_id})", dry_run)
            j["status"] = "done"
            daemon_state["current"] = None
            continue
        if status == CHECKPOINT_STATUS:
            if should_notify(daemon_state, case_id, "checkpoint"):
                notify("NeedlePoint", f"{j['npi']} needs a human checkpoint ({case_id})", dry_run)
            j["status"] = "checkpoint"      # freed; awaits human answer_checkpoint
            daemon_state["current"] = None
            continue

        # stall / crash detection
        alive = tmux_alive(session, dry_run)
        stalled = case_dir is not None and is_stalled(case_dir, _log_path(j["npi"]), now)
        if (stalled and alive) or (not alive and status not in TERMINAL_STATUSES):
            if is_calibration(case_id):
                if should_notify(daemon_state, case_id, "calib_stall"):
                    notify("NeedlePoint", f"calibration {case_id} stalled — manual attention", dry_run)
                j["status"] = "parked"
                daemon_state["current"] = None
                continue
            if j.get("resume_count", 0) >= MAX_RESUMES:
                if should_notify(daemon_state, case_id, "parked"):
                    notify("NeedlePoint", f"{j['npi']} parked after {MAX_RESUMES} auto-resumes ({case_id})", dry_run)
                j["status"] = "parked"
                daemon_state["current"] = None
                continue
            # auto-resume
            tmux_kill(session, dry_run)
            tmux_start(session, j["npi"], case_id, dry_run)
            j["resume_count"] = j.get("resume_count", 0) + 1
            j["started_at"] = now
        return jobs, daemon_state   # one running job at a time; done monitoring

    # 2. nothing running → claim the next queued job
    for j in jobs:
        if j["status"] == "queued":
            session = f"npq-{j['npi']}"
            tmux_start(session, j["npi"], j.get("resume"), dry_run)
            j["status"] = "running"
            j["started_at"] = now
            daemon_state["current"] = j["npi"]
            return jobs, daemon_state
    return jobs, daemon_state


def cmd_daemon(concurrency: int, poll: int, dry_run: bool = False, max_ticks: int | None = None) -> None:
    if concurrency != 1:
        print(f"WARNING: --concurrency {concurrency} runs multiple investigations at once, "
              f"which drains the shared 5-hour subscription token window faster and is NOT "
              f"recommended. Sequential (1) is the supported mode.", file=sys.stderr)
    print(f"np_queue daemon: concurrency={concurrency} poll={poll}s stall={STALL_SECONDS}s"
          + (" [DRY-RUN]" if dry_run else ""))
    ticks = 0
    while True:
        jobs = load_queue()
        st = load_daemon_state()
        jobs, st = daemon_tick(jobs, st, time.time(), dry_run)
        save_queue(jobs)
        save_daemon_state(st)
        active = [j for j in jobs if j["status"] in ("queued", "running")]
        if not active:
            print("queue drained — daemon exiting")
            return
        ticks += 1
        if max_ticks is not None and ticks >= max_ticks:
            return
        if not dry_run:
            time.sleep(poll)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add"); a.add_argument("--npi", required=True); a.add_argument("--resume", default=None)
    sub.add_parser("list")
    sub.add_parser("status")
    d = sub.add_parser("daemon")
    d.add_argument("--concurrency", type=int, default=1, help="runs at once (default 1; >1 not recommended)")
    d.add_argument("--poll", type=int, default=120)
    d.add_argument("--dry-run", action="store_true")
    d.add_argument("--max-ticks", type=int, default=None, help="stop after N ticks (testing)")
    args = ap.parse_args(argv)

    if args.cmd == "add":
        cmd_add(args.npi.strip(), args.resume)
    elif args.cmd == "list":
        cmd_list()
    elif args.cmd == "status":
        cmd_status()
    elif args.cmd == "daemon":
        cmd_daemon(args.concurrency, args.poll, args.dry_run, args.max_ticks)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
