#!/usr/bin/env python3
"""NeedlePoint: run a Medicare fraud investigation on any provider from one command.

    python3 needlepoint.py                       # guided: pick settings, type an NPI, run
    python3 needlepoint.py run --npi 1234567890  # same, with flags (add --yes to skip prompts)
    python3 needlepoint.py knobs                 # list every setting and its current value
    python3 needlepoint.py doctor                # check your setup
    python3 needlepoint.py show [CASE-ID]        # summarize a finished case (default: latest)
    python3 needlepoint.py fetch --year 2024     # download the public CMS data a case needs

Output is a hypothesis for review, not a finding that anyone committed fraud.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent
STATE = REPO / "fraud-investigator-v2" / "state"
DATA = REPO / "data" / "processed"
LOGS = REPO / "logs"
SETTINGS_FILE = REPO / ".needlepoint.json"   # remembers your last choices (git-ignored)
PY = sys.executable

try:  # keys and settings may live in .env
    from dotenv import load_dotenv
    load_dotenv(REPO / ".env", override=False)
except ImportError:
    pass

PROVIDER_KEYS = {"ANTHROPIC_API_KEY": "anthropic", "OPENAI_API_KEY": "openai", "GEMINI_API_KEY": "gemini",
                 "GOOGLE_API_KEY": "gemini", "OPENROUTER_API_KEY": "openrouter", "MISTRAL_API_KEY": "mistral"}
# Suggested models per provider (any LiteLLM "provider/model" string works; override with --model).
SUGGESTED = {
    "anthropic": ("anthropic/claude-sonnet-5", "anthropic/claude-opus-5"),
    "openai": ("openai/gpt-5", "openai/gpt-5"),
    "gemini": ("gemini/gemini-2.5-pro", "gemini/gemini-2.5-pro"),
    "openrouter": ("openrouter/anthropic/claude-sonnet-5", "openrouter/anthropic/claude-opus-5"),
    "mistral": ("mistral/mistral-large-latest", "mistral/mistral-large-latest"),
}
TOOL_STEPS = ["investigator_discover", "investigator", "rebuttal_investigator", "case_writer"]
REASON_STEPS = ["counsel_build", "counsel_first_chair_review", "counsel_rebuttal", "counsel_disposition",
                "defense", "foreclosure_check", "legal_adv_response"]
DEPTHS = {  # (build iterations, adversarial rounds). The loop needs >= 2 build iterations to reach a verdict.
    "quick": (2, 1, "about 2-3 h; one defense round"),
    "standard": (3, 2, "about 3-5 h"),
    "full": (3, 4, "about 4-8 h; the full adversarial gauntlet"),
}
EFFORTS = ["low", "medium", "high", "max"]
DATA_YEARS = list(range(2013, 2025))


# ── setup checks ──────────────────────────────────────────────────────────────
def available_providers() -> list[str]:
    seen = []
    for var, prov in PROVIDER_KEYS.items():
        if os.environ.get(var) and prov not in seen:
            seen.append(prov)
    return seen


def claude_model(m: str) -> str:
    """Claude Code takes Claude IDs or aliases (sonnet, opus); drop a LiteLLM provider prefix."""
    return m.split("/", 1)[1] if m.startswith("anthropic/") else m


def claude_cli() -> str | None:
    return os.environ.get("CLAUDE_BIN") or shutil.which("claude")


def default_settings() -> dict:
    provs = available_providers()
    backend = "api" if (provs or os.environ.get("FRAUD_INV_API_BASE")) else ("claude" if claude_cli() else "api")
    tool_m, reason_m = SUGGESTED.get(provs[0], (None, None)) if provs else (None, None)
    s = {"backend": backend, "model": None, "tool_model": tool_m, "reason_model": reason_m,
         "effort": None, "depth": "quick", "year": max(DATA_YEARS), "api_base": os.environ.get("FRAUD_INV_API_BASE")}
    if SETTINGS_FILE.exists():
        try:
            s.update({k: v for k, v in json.loads(SETTINGS_FILE.read_text()).items() if k in s})
        except json.JSONDecodeError:
            pass
    return s


def data_status(year: int) -> dict[str, str]:
    """Per table: "local" (downloaded), "remote" (readable in place from the public mirror or
    your bucket, nothing to download), or "missing"."""
    sys.path.insert(0, str(REPO))
    try:
        from fraudlib import r2
    except Exception:
        r2 = None
    out = {}
    for name in ("cms_partb", "cms_partb_svc"):
        p = DATA / f"{name}_{year}.parquet"
        src = r2.locate(p) if r2 else (str(p) if p.exists() else None)
        out[name] = "missing" if not src else ("local" if src == str(p) else "remote")
    return out


def _status_line(year: int) -> str:
    return ", ".join(f"{k}={v}" for k, v in data_status(year).items())


def problems(s: dict) -> list[str]:
    out = []
    if s["backend"] == "api":
        try:
            import litellm  # noqa: F401
        except ImportError:
            out.append("api backend needs LiteLLM: pip install -r requirements.txt")
        if not (available_providers() or s.get("api_base")):
            out.append("no model key found: set ANTHROPIC_API_KEY (or OPENAI_API_KEY, GEMINI_API_KEY, ...) "
                       "in .env, or pass --api-base for a local model server")
        if not (s.get("model") or s.get("tool_model")):
            out.append("no model chosen: pass --model provider/model (e.g. anthropic/claude-sonnet-5)")
    else:
        if not claude_cli():
            out.append("claude backend needs the Claude Code CLI (https://claude.com/claude-code); "
                       "or use --backend api with your own key")
        for m in (s.get("model"), s.get("tool_model"), s.get("reason_model")):
            if m and "/" in m and not m.startswith("anthropic/"):
                out.append(f"claude backend only runs Claude models; {m} needs --backend api")
    if all(v == "missing" for v in data_status(s["year"]).values()):
        out.append(f"no CMS data for {s['year']}: set NP_DATA_URL to the public mirror (see .env.example), "
                   f"or download it:  python3 needlepoint.py fetch --year {s['year']}")
    return out


# ── NPI lookup ────────────────────────────────────────────────────────────────
def lookup_npi(npi: str) -> dict:
    url = f"https://npiregistry.cms.hhs.gov/api/?version=2.1&number={npi}"
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "needlepoint"}), timeout=30) as r:
        d = json.load(r)
    if not d.get("result_count"):
        raise SystemExit(f"NPI {npi} was not found in the NPPES registry.")
    res = d["results"][0]
    b = res.get("basic", {})
    loc = next((a for a in res.get("addresses", []) if a.get("address_purpose") == "LOCATION"), {})
    tax = next((t for t in res.get("taxonomies", []) if t.get("primary")), {})
    name = b.get("organization_name") or " ".join(x for x in (b.get("first_name"), b.get("last_name")) if x)
    return {"npi": npi, "name": name.title(), "credential": b.get("credential") or "",
            "specialty": tax.get("desc") or "", "city": (loc.get("city") or "").title(),
            "state": loc.get("state") or "", "is_org": res.get("enumeration_type") == "NPI-2"}


def subject_string(p: dict) -> str:
    parts = [p["name"] + (f", {p['credential']}" if p["credential"] else ""), p["specialty"],
             f"{p['city']}, {p['state']}".strip(", "), f"NPI {p['npi']}"]
    return " -- ".join(x for x in parts if x)


# ── knobs ─────────────────────────────────────────────────────────────────────
def describe(s: dict) -> list[tuple[str, str, str]]:
    b_it, a_r, dur = DEPTHS[s["depth"]]
    models = (s["model"] or f"tool steps: {s['tool_model'] or 'built-in'} | reasoning steps: "
              f"{s['reason_model'] or 'built-in'}")
    return [
        ("backend", s["backend"], "api = any provider with your key; claude = Claude Code CLI"),
        ("models", models, "--model for every step, or --tool-model / --reason-model"),
        ("effort", s["effort"] or "built-in per step", "low | medium | high | max"),
        ("depth", f"{s['depth']} ({b_it} build iterations, {a_r} defense round{'s' if a_r > 1 else ''})", f"quick | standard | full; {dur}"),
        ("data year", str(s["year"]), f"{DATA_YEARS[0]}-{DATA_YEARS[-1]}; status: {_status_line(s['year'])}"),
        ("local model server", s["api_base"] or "none", "--api-base http://localhost:11434 (Ollama, vLLM, ...)"),
    ]


def print_knobs(s: dict) -> None:
    for i, (k, v, help_) in enumerate(describe(s), 1):
        print(f"  {i}. {k:18s} {v}\n     {'':18s} {help_}")


def edit_knobs(s: dict) -> dict:
    while True:
        print("\nSettings:")
        print_knobs(s)
        choice = input("\nPress Enter to continue, or a number to change a setting: ").strip()
        if not choice:
            return s
        if choice == "1":
            s["backend"] = _ask("backend (api/claude)", s["backend"], ["api", "claude"])
        elif choice == "2":
            m = input("  one model for every step (provider/model), or Enter to set two: ").strip()
            if m:
                s["model"] = m
            else:
                s["model"] = None
                s["tool_model"] = input(f"  model for data-gathering steps [{s['tool_model']}]: ").strip() or s["tool_model"]
                s["reason_model"] = input(f"  model for judgment steps [{s['reason_model']}]: ").strip() or s["reason_model"]
        elif choice == "3":
            e = input("  effort (low/medium/high/max, Enter = built-in): ").strip()
            s["effort"] = e if e in EFFORTS else None
        elif choice == "4":
            s["depth"] = _ask("depth (quick/standard/full)", s["depth"], list(DEPTHS))
        elif choice == "5":
            y = input(f"  data year [{s['year']}]: ").strip()
            s["year"] = int(y) if y.isdigit() and int(y) in DATA_YEARS else s["year"]
        elif choice == "6":
            s["api_base"] = input("  local model server URL (Enter = none): ").strip() or None


def _ask(label: str, cur: str, options: list[str]) -> str:
    v = input(f"  {label} [{cur}]: ").strip().lower()
    return v if v in options else cur


# ── run ───────────────────────────────────────────────────────────────────────
def run_env(s: dict) -> dict:
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([str(REPO), str(REPO / "fraud-investigator-v2" / "src"),
                                         env.get("PYTHONPATH", "")])
    env["FRAUD_INV_EXEC_BACKEND"] = "api" if s["backend"] == "api" else "claude_p"
    if s.get("api_base"):
        env["FRAUD_INV_API_BASE"] = s["api_base"]
    prefix = "FRAUD_INV_API_MODEL_" if s["backend"] == "api" else "FRAUD_INV_MODEL_"
    for steps, m in ((TOOL_STEPS, s["model"] or s["tool_model"]), (REASON_STEPS, s["model"] or s["reason_model"])):
        if m and s["backend"] == "claude":
            m = claude_model(m)
        if m:
            for step in steps:
                env[prefix + step.upper()] = m
    if s.get("effort"):
        for step in TOOL_STEPS + REASON_STEPS + ["triage"]:
            env["FRAUD_INV_EFFORT_" + step.upper()] = s["effort"]
    env.setdefault("PYTHONUNBUFFERED", "1")
    # Per-step wall clock. Remote data reads and web searches make an investigator step run
    # well past the 600 s engine default; 1800 s matches what calibration runs use.
    env.setdefault("CLAUDE_TIMEOUT_SEC", "1800")
    return env


PROGRESS = re.compile(r"(=== (Phase|Pass)[^=]*===|\[(\w+)\] attempt \d+ OK \(([\d.]+)s|verdict=|HITL CHECKPOINT|"
                      r"COMPLETE —|New case:|Resuming case|Traceback|Error:)")


def run_case(subject: str, s: dict, case_id: str | None, log_path: Path) -> str | None:
    iters, rounds, _ = DEPTHS[s["depth"]]
    cmd = [PY, "-m", "fraud_investigator", subject, "--max-pre-rebuttal-iters", str(iters),
           "--max-adversarial", str(rounds), "-v"]
    if case_id:
        cmd += ["--case-id", case_id]
    LOGS.mkdir(exist_ok=True)
    found = case_id
    started = time.time()
    with log_path.open("a", buffering=1) as log, subprocess.Popen(cmd, cwd=REPO, env=run_env(s), stdout=subprocess.PIPE,
                                                    stderr=subprocess.STDOUT, text=True) as proc:
        for line in proc.stdout:
            log.write(line)
            m = PROGRESS.search(line)
            if not m:
                continue
            cid = re.search(r"(CASE-[0-9A-F]{8}-\d{4})", line)
            if cid and not found:
                found = cid.group(1)
            mins = (time.time() - started) / 60
            if m.group(3):
                print(f"  [{mins:5.1f} min] {m.group(3)} done ({float(m.group(4)):.0f}s)")
            elif "Traceback" in line or "Error:" in line:
                print(f"  ! {line.strip()[:160]}")
            else:
                print(f"  [{mins:5.1f} min] {line.split(' INFO ')[-1].split(' WARNING ')[-1].strip()[:140]}")
        rc = proc.wait()
    if rc != 0:
        print(f"\nThe run exited with code {rc}. Full log: {log_path}")
    return found


def open_checkpoint(case_id: str) -> dict | None:
    p = STATE / case_id / "summary.json"
    if not p.exists():
        return None
    rr = json.loads(p.read_text()).get("review_request") or {}
    return rr if rr and not rr.get("resolved") else None


def handle_checkpoint(case_id: str, rr: dict, interactive: bool) -> bool:
    """Return True if answered (so the caller resumes)."""
    print(f"\nThe case paused for a human decision ({rr.get('checkpoint_type')}):\n")
    print("  " + str(rr.get("question", "")).replace("\n", "\n  ")[:1500])
    cmd = f"python3 scripts/answer_checkpoint.py answer {case_id} --resolution ... --answer \"...\""
    if not interactive:
        print(f"\nAnswer it later with:\n  {cmd}\nthen resume:  python3 needlepoint.py run --resume {case_id}")
        return False
    print("\n  d = the innocent explanation holds, decline (the honest default if you cannot verify more)")
    print("  f = you verified facts that rule it out (you will be asked for them)")
    print("  s = stop here and decide later")
    c = input("Choice [d/f/s]: ").strip().lower()
    if c not in ("d", "f"):
        print(f"Stopped. Later:\n  {cmd}\n  python3 needlepoint.py run --resume {case_id}")
        return False
    res = "affirm_decline" if c == "d" else "foreclose_explanation"
    ans = input("What did you verify (one or two sentences)? ").strip() or \
        "Reviewer could not verify facts that rule out the innocent explanation."
    cite = input("Source (URL or document), optional: ").strip()
    args = [PY, "scripts/answer_checkpoint.py", "answer", case_id, "--resolution", res, "--answer", ans,
            "--reviewer", "needlepoint-cli"]
    if cite:
        args += ["--citation", cite]
    return subprocess.call(args, cwd=REPO) == 0


def cmd_run(a: argparse.Namespace) -> int:
    s = default_settings()
    for k in ("backend", "model", "tool_model", "reason_model", "effort", "depth", "year", "api_base"):
        v = getattr(a, k, None)
        if v is not None:
            s[k] = v
    if s["backend"] == "api" and not (s["model"] or s["tool_model"]) and available_providers():
        s["tool_model"], s["reason_model"] = SUGGESTED[available_providers()[0]]
    interactive = not a.yes and sys.stdin.isatty()

    print(__doc__.splitlines()[0] + "\n")
    if interactive and not a.resume:
        s = edit_knobs(s)
    issues = problems(s)
    if issues:
        print("\nSetup needed before running:")
        for i in issues:
            print(f"  - {i}")
        return 1
    SETTINGS_FILE.write_text(json.dumps(s, indent=1))

    case_id = a.resume
    if case_id:
        summ = json.loads((STATE / case_id / "summary.json").read_text())
        subject = summ["subject"]
    else:
        npi = a.npi or (input("\nNPI to investigate (10 digits): ").strip() if interactive else "")
        if not re.fullmatch(r"\d{10}", npi or ""):
            print("Give a 10-digit NPI: python3 needlepoint.py run --npi 1234567890")
            return 1
        p = lookup_npi(npi)
        subject = subject_string(p)
        print(f"\nProvider: {subject}")
        if p["is_org"]:
            print("  note: this is an organization NPI; the loop is tuned for individual clinicians.")
        if interactive and input("Run the investigation? [Y/n]: ").strip().lower() in ("n", "no"):
            return 0

    iters, rounds, dur = DEPTHS[s["depth"]]
    print(f"\nRunning ({s['depth']}: {iters} build iterations, {rounds} defense round{'s' if rounds > 1 else ''}; {dur}).")
    print("Progress below; the full log is in logs/. Ctrl-C stops; resume with --resume CASE-ID.\n")
    log_path = LOGS / f"case_{time.strftime('%Y%m%d_%H%M%S')}.log"
    case_id = run_case(subject, s, case_id, log_path)
    while case_id:
        rr = open_checkpoint(case_id)
        if not rr or not handle_checkpoint(case_id, rr, interactive):
            break
        print("\nResuming (finished steps load from cache)...\n")
        run_case(subject, s, case_id, log_path)
    if case_id:
        print()
        subprocess.call([PY, "scripts/show_case.py", case_id], cwd=REPO)
        print(f"\nRead the report: {STATE / case_id / '00_README.md'}  (then 01_Case_Report.md, "
              f"02_Layperson_Summary.md)")
    return 0


# ── other commands ────────────────────────────────────────────────────────────
def cmd_knobs(_: argparse.Namespace) -> int:
    s = default_settings()
    print("Current settings (change with flags on `run`, in the guided menu, or in .env):\n")
    print_knobs(s)
    print("\nAdvanced knobs: see .env.example and CONTRIBUTING.md (prompts, verdict logic, registries).")
    return 0


def cmd_doctor(_: argparse.Namespace) -> int:
    s = default_settings()
    print(f"python        {sys.version.split()[0]} ({'ok' if sys.version_info >= (3, 11) else 'need 3.11+'})")
    print(f"provider keys {', '.join(available_providers()) or 'none'}")
    print(f"claude CLI    {claude_cli() or 'not installed (only needed for --backend claude)'}")
    try:
        import litellm  # noqa: F401
        print("litellm       installed")
    except ImportError:
        print("litellm       missing (pip install -r requirements.txt)")
    print(f"data {s['year']}     {_status_line(s['year'])}")
    issues = problems(s)
    print("\n" + ("Ready: python3 needlepoint.py" if not issues else "To fix:\n  - " + "\n  - ".join(issues)))
    return 0 if not issues else 1


def cmd_show(a: argparse.Namespace) -> int:
    args = [PY, "scripts/show_case.py"] + ([a.case] if a.case else ["--latest"])
    return subprocess.call(args, cwd=REPO)


def cmd_fetch(a: argparse.Namespace) -> int:
    datasets = a.datasets or ["partb", "partb_svc"]
    print(f"Downloading CMS {', '.join(datasets)} for {a.year} into data/processed/ "
          "(by-provider is about 0.5 GB; by-service is several GB).")
    return subprocess.call([PY, "scripts/fetch_public_data.py", "--years", str(a.year), "--datasets", *datasets],
                           cwd=REPO)


def main() -> int:
    ap = argparse.ArgumentParser(prog="needlepoint", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd")
    r = sub.add_parser("run", help="investigate one provider")
    r.add_argument("--npi")
    r.add_argument("--backend", choices=["api", "claude"])
    r.add_argument("--model", help="one LiteLLM model for every step, e.g. anthropic/claude-sonnet-5")
    r.add_argument("--tool-model", help="model for data-gathering steps")
    r.add_argument("--reason-model", help="model for judgment steps (counsel, defense, foreclosure, verdict)")
    r.add_argument("--effort", choices=EFFORTS)
    r.add_argument("--depth", choices=list(DEPTHS))
    r.add_argument("--year", type=int, choices=DATA_YEARS)
    r.add_argument("--api-base", help="local OpenAI-compatible server, e.g. http://localhost:11434")
    r.add_argument("--resume", metavar="CASE-ID")
    r.add_argument("--yes", action="store_true", help="no prompts (checkpoints are left for you to answer)")
    r.set_defaults(fn=cmd_run)
    sub.add_parser("knobs", help="list settings").set_defaults(fn=cmd_knobs)
    sub.add_parser("doctor", help="check setup").set_defaults(fn=cmd_doctor)
    sh = sub.add_parser("show", help="summarize a case")
    sh.add_argument("case", nargs="?")
    sh.set_defaults(fn=cmd_show)
    f = sub.add_parser("fetch", help="download CMS data")
    f.add_argument("--year", type=int, default=max(DATA_YEARS), choices=DATA_YEARS)
    f.add_argument("--datasets", nargs="+", choices=["partb", "partb_svc", "partd", "dmepos", "open_payments"])
    f.set_defaults(fn=cmd_fetch)
    a = ap.parse_args()
    if not a.cmd:  # bare invocation = guided run
        a = ap.parse_args(["run"])
    try:
        return a.fn(a)
    except KeyboardInterrupt:
        print("\nStopped. Resume a case with: python3 needlepoint.py run --resume CASE-ID")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
