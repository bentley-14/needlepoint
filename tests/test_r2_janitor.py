"""R2 janitor — verification-gated local cleanup (synthetic, no rclone)."""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.r2_janitor import execute, plan

DAY = 86400


def _mk(root: Path, rel: str, age_days: float, now: float) -> Path:
    d = root / rel
    d.mkdir(parents=True)
    f = d / "summary.json"
    f.write_text("{}")
    ts = now - age_days * DAY
    import os
    os.utime(f, (ts, ts))
    os.utime(d, (ts, ts))
    return d


def _repo(tmp_path):
    now = time.time()
    _mk(tmp_path, "fraud-investigator-v2/state/CASE-OLD", 60, now)
    _mk(tmp_path, "fraud-investigator-v2/state/CASE-YOUNG", 2, now)
    _mk(tmp_path, "fraud-investigator-v2/state/CASE-PARKED-OLD", 90, now)
    _mk(tmp_path, "state/runs/run_old", 45, now)
    pk = tmp_path / "fraud-investigator-v2/state/parked"
    pk.mkdir(parents=True)
    (pk / "CASE-PARKED-OLD.json").write_text(json.dumps(
        {"case_id": "CASE-PARKED-OLD", "npi": "1234567893"}))
    return tmp_path, now


def test_plan_age_and_parked_rules(tmp_path):
    root, now = _repo(tmp_path)
    actions = {a["dir"].split("/")[-1]: a for a in
               plan(root, ttl_days=28, now=now)}
    assert actions["CASE-OLD"]["action"] == "CANDIDATE"
    assert actions["run_old"]["action"] == "CANDIDATE"
    assert actions["CASE-YOUNG"]["action"] == "KEEP"
    assert "young" in actions["CASE-YOUNG"]["why"]
    assert actions["CASE-PARKED-OLD"]["action"] == "KEEP"
    assert actions["CASE-PARKED-OLD"]["why"] == "parked case"
    # the parked registry itself is never a target
    assert "parked" not in {Path(a["dir"]).name for a in actions.values() if a["action"] == "CANDIDATE"}


def test_execute_requires_verification(tmp_path):
    root, now = _repo(tmp_path)
    actions = plan(root, ttl_days=28, now=now)
    res = {a["dir"].split("/")[-1]: a for a in
           execute(actions, apply=True, verifier=lambda local, remote: False)}
    assert res["CASE-OLD"]["action"] == "KEEP"
    assert "VERIFICATION FAILED" in res["CASE-OLD"]["why"]
    assert (root / "fraud-investigator-v2/state/CASE-OLD").exists()  # untouched


def test_execute_dry_run_never_deletes(tmp_path):
    root, now = _repo(tmp_path)
    res = {a["dir"].split("/")[-1]: a for a in
           execute(plan(root, ttl_days=28, now=now), apply=False,
                   verifier=lambda l, r: True)}
    assert res["CASE-OLD"]["action"] == "WOULD_DELETE"
    assert (root / "fraud-investigator-v2/state/CASE-OLD").exists()


def test_execute_apply_deletes_only_verified_candidates(tmp_path):
    root, now = _repo(tmp_path)
    verified = {"CASE-OLD"}  # run_old fails verification
    res = {a["dir"].split("/")[-1]: a for a in execute(
        plan(root, ttl_days=28, now=now), apply=True,
        verifier=lambda local, remote: Path(local).name in verified)}
    assert res["CASE-OLD"]["action"] == "DELETED"
    assert not (root / "fraud-investigator-v2/state/CASE-OLD").exists()
    assert res["run_old"]["action"] == "KEEP"
    assert (root / "state/runs/run_old").exists()
    assert (root / "fraud-investigator-v2/state/CASE-PARKED-OLD").exists()


def test_remote_paths_mirror_sync_script(tmp_path):
    root, now = _repo(tmp_path)
    by = {a["dir"].split("/")[-1]: a for a in plan(root, ttl_days=28, now=now)}
    assert by["CASE-OLD"]["remote"] == "r2:needlepoint-data/artifacts/cases/CASE-OLD"
    assert by["run_old"]["remote"] == "r2:needlepoint-data/artifacts/pipeline_runs/run_old"
