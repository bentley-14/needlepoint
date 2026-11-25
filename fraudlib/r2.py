"""Thin rclone wrapper for R2 (Cloudflare) access.

Bucket and remote are configurable: NP_R2_BUCKET (default needlepoint-data), NP_R2_REMOTE (default r2).
Install rclone and configure the r2: remote via `bash scripts/setup_r2.sh`.

Usage:
    from fraudlib.r2 import r2_copy, r2_exists

    r2_copy("r2:needlepoint-data/cache/v1/feature_frames/features_full_2022.parquet",
            "data/cache/feature_frames/features_full_2022.parquet")
    r2_exists("r2:needlepoint-data/cache/v1/feature_frames/features_full_2022.parquet")
"""
from __future__ import annotations

import configparser
import functools
import os
import subprocess
import sys
from pathlib import Path

# Storage location is configurable so anyone can point the platform at their own
# S3-compatible bucket (Cloudflare R2, AWS S3, MinIO, ...). With no rclone remote
# configured, every tool falls back to local files under data/.
#   NP_R2_REMOTE  rclone remote name holding the credentials   (default: r2)
#   NP_R2_BUCKET  bucket name                                  (default: needlepoint-data)
_REMOTE_NAME = os.environ.get("NP_R2_REMOTE", "r2").strip() or "r2"
BUCKET = os.environ.get("NP_R2_BUCKET", "needlepoint-data").strip() or "needlepoint-data"
R2_REMOTE = f"{_REMOTE_NAME}:{BUCKET}"
# Canonical normalized panel store (Pandera-recast, schema-consistent across years —
# see pipeline/scrapers/*.py + pipeline/scripts/recast_r2_dtypes.py).
R2_PANELS = f"{R2_REMOTE}/processed/v1"
# Keys written in code use the default spelling; _canon() maps them onto the configured remote.
_DEFAULT_PREFIX = "r2:needlepoint-data"

# Public read-only mirror: a copy of the bucket layout that anyone can read without
# credentials, e.g. hf://datasets/<owner>/needlepoint-data (Hugging Face) or an https base
# URL. Used only when no rclone remote is configured; a private bucket always wins.
# Set NP_DATA_URL="" to turn it off (local files only).
PUBLIC_DATA_URL = "hf://datasets/bentley14/needlepoint-data"
DATA_URL = os.environ.get("NP_DATA_URL", PUBLIC_DATA_URL).strip().rstrip("/")
_HF = "hf://datasets/"


def _canon(key: str) -> str:
    """Map a code-default key (``r2:needlepoint-data/...``) onto the configured remote/bucket."""
    key = str(key)
    if key.startswith(_DEFAULT_PREFIX):
        return R2_REMOTE + key[len(_DEFAULT_PREFIX):]
    if key.startswith("s3://needlepoint-data"):
        return f"s3://{BUCKET}" + key[len("s3://needlepoint-data"):]
    return key

_SETUP_HINT = "run bash scripts/setup_r2.sh"


# ── duckdb httpfs reads (point reads against the normalized R2 panels) ─────────
# Tools read R2-FIRST by default (R2 = source of truth): the canonical normalized
# parquet is read straight from R2 via duckdb httpfs with NPI/column pushdown — fetch
# the rows you need, not the file. A local copy is the fallback. Set NP_R2_FIRST=0 for
# local-first (faster on a data-laden machine); NP_R2_DISABLE=1 forces local-only
# (calibration fail-closed). See parquet_source() / fetch_local().

def _rclone_r2_creds() -> dict:
    """Read the r2: S3 credentials from the rclone config (no secret echoing)."""
    cfg = configparser.ConfigParser()
    cfg.read(os.path.expanduser("~/.config/rclone/rclone.conf"))
    if _REMOTE_NAME not in cfg:
        raise RuntimeError(f"no [r2] remote in rclone config — {_SETUP_HINT}")
    r = cfg[_REMOTE_NAME]
    ep = (r.get("endpoint", "") or "").replace("https://", "").replace("http://", "")
    return {"key": r.get("access_key_id", ""), "secret": r.get("secret_access_key", ""), "endpoint": ep}


def attach_r2(con) -> None:
    """Configure httpfs + the R2 S3 secret on a duckdb connection (idempotent). The public
    mirror needs httpfs only."""
    con.execute("INSTALL httpfs; LOAD httpfs;")
    if _public():
        # Hugging Face rate-limits anonymous reads (HTTP 429, Retry-After ~90 s): back off
        # instead of failing. An optional HF_TOKEN (free read token) raises the limit.
        con.execute("SET http_retries=6; SET http_retry_wait_ms=5000; SET http_retry_backoff=2;")
        if os.environ.get("HF_TOKEN") and DATA_URL.startswith(_HF):
            con.execute("CREATE OR REPLACE SECRET hf (TYPE HUGGINGFACE, TOKEN ?);",
                        [os.environ["HF_TOKEN"]])
        return
    c = _rclone_r2_creds()
    con.execute(
        "CREATE OR REPLACE SECRET r2 (TYPE S3, KEY_ID ?, SECRET ?, ENDPOINT ?, REGION 'auto', URL_STYLE 'path');",
        [c["key"], c["secret"], c["endpoint"]],
    )


@functools.lru_cache(maxsize=1)
def duckdb_r2():
    """A process-wide duckdb connection with httpfs + the R2 secret attached.

    The secret is attached only when the rclone [r2] remote is configured and
    NP_R2_DISABLE is unset — mirroring parquet_source()'s local fallback.
    Without this guard, a machine with no rclone config (CI, a fresh teammate
    checkout) cannot read even LOCAL parquets through this connection:
    attach_r2 raises at connect time, before any query runs. When R2 is
    unconfigured, queries on local paths succeed and queries on s3:// sources
    fail at query time (correctly).
    """
    import duckdb
    con = duckdb.connect()
    if not _r2_disabled() and _r2_configured():
        attach_r2(con)
    return con


def _s3_url(r2_key: str) -> str:
    """<remote>:<bucket>/processed/v1/x.parquet -> s3://<bucket>/processed/v1/x.parquet
    (or the public mirror's URL when reading from NP_DATA_URL)."""
    if _public():
        return _public_url(r2_key)
    return "s3://" + _canon(r2_key).split(":", 1)[1]


def _r2_disabled() -> bool:
    """Fail-closed switch. When NP_R2_DISABLE=1 (set by the calibration launcher), NEVER reach
    R2 — a jailed/blind run must read ONLY pre-staged cutoff-clean local copies, so a missing
    panel fails cleanly instead of fetching post-cutoff data from R2 (leak)."""
    return os.environ.get("NP_R2_DISABLE", "0").strip() == "1"


def _r2_first() -> bool:
    """R2-first read precedence (default ON): treat R2 as the source of truth and read
    from it directly, using a local copy only as a fallback. Set NP_R2_FIRST=0 for the
    legacy local-first behavior (read local when present — faster on a data-laden
    machine; used by the calibration builder's bulk staging). NP_R2_DISABLE always
    overrides this (→ local only)."""
    return os.environ.get("NP_R2_FIRST", "1").strip() != "0"


def _rclone_configured() -> bool:
    """True if the rclone remote + access key are present. Cheap: reads the rclone config."""
    try:
        return bool(_rclone_r2_creds().get("key"))
    except Exception:
        return False


def _public() -> bool:
    """Read from the public mirror (NP_DATA_URL) because no private bucket is configured."""
    return bool(DATA_URL) and not _rclone_configured()


def _r2_configured() -> bool:
    """True if a remote data source exists: a private rclone bucket or the public mirror.
    R2-first falls back to local when neither is set up."""
    return _rclone_configured() or bool(DATA_URL)


def _rel(key: str) -> str:
    """Path inside the bucket: r2:<bucket>/processed/v1/x.parquet -> processed/v1/x.parquet"""
    k = _canon(key)
    k = k[len("s3://"):] if k.startswith("s3://") else k.split(":", 1)[-1]
    return k.split("/", 1)[1] if "/" in k else ""


def _public_url(key: str, *, http: bool = False) -> str:
    """Location of a bucket key on the public mirror. DuckDB reads hf:// natively; plain
    downloads use the https form. A file:// mirror (tests) maps to a local path for DuckDB."""
    rel = _rel(key)
    if http and DATA_URL.startswith(_HF):
        return f"https://huggingface.co/datasets/{DATA_URL[len(_HF):]}/resolve/main/{rel}"
    if not http and DATA_URL.startswith("file://"):
        return f"{DATA_URL[len('file://'):]}/{rel}"
    return f"{DATA_URL}/{rel}"


@functools.lru_cache(maxsize=1)
def _public_index() -> frozenset[str]:
    """Every path on the public mirror, from the _index.json the publisher writes. Empty on
    any failure, so callers fall back to local files."""
    import json
    import time
    import urllib.error
    import urllib.request
    url = _public_url(f"{_DEFAULT_PREFIX}/_index.json", http=True)
    for wait in (5, 30, 90, None):          # 429 / 5xx: back off and retry
        try:
            with urllib.request.urlopen(url, timeout=30) as r:
                return frozenset(json.load(r)["files"])
        except urllib.error.HTTPError as e:
            if wait is None or not (e.code == 429 or e.code >= 500):
                break
            time.sleep(wait)
        except Exception:
            break
    print(f"warning: could not read the public data index ({url}); using local files only",
          file=sys.stderr)
    return frozenset()


def _r2_first_active(r2_first: bool | None) -> bool:
    """Resolve the effective R2-first decision for one call. Explicit r2_first arg wins
    over the NP_R2_FIRST env default; NP_R2_DISABLE (fail-closed) always forces False;
    R2 must be configured for R2-first to engage (else fall back to local)."""
    if _r2_disabled() or _public():   # the public mirror is a fallback: local files win
        return False
    use = _r2_first() if r2_first is None else r2_first
    return use and _r2_configured()


def _table_cache() -> Path:
    return Path(os.environ.get("NP_R2_TABLE_CACHE",
                               str(Path.home() / ".cache" / "fraud-detection" / "r2_tables")))


def parquet_source(local_path: str | Path, r2_key: str, *, r2_first: bool | None = None) -> str:
    """Resolve the duckdb read_parquet() source for a panel.

    R2-first (default; NP_R2_FIRST!=0): R2 is the source of truth → return the s3:// URL
    whenever R2 is configured, reading the canonical normalized panel directly via httpfs.
    Local is the fallback (R2 unconfigured, or only the local copy is available). Pass
    r2_first=False (or NP_R2_FIRST=0) for local-first — faster on a data-laden machine;
    used by the calibration builder.

    NP_R2_DISABLE=1 overrides everything → always the local path (calibration fail-closed;
    a jailed run must never reach R2). Caller reads via a connection with attach_r2()
    applied (or use duckdb_r2())."""
    p = Path(local_path)
    if _r2_first_active(r2_first):
        return _s3_url(r2_key)                  # R2 as source of truth
    if p.exists() or _r2_disabled():
        return str(p)                           # local: local-first hit, fallback, or fail-closed
    return _s3_url(r2_key)                       # local absent → R2 (last resort)


def fetch_local(local_path: str | Path, r2_key: str, *, r2_first: bool | None = None) -> Path:
    """Ensure a readable LOCAL file for pandas/pyarrow readers, return its path.

    R2-first (default): pull the canonical file from R2 into the table cache and return
    that (R2 = source of truth), falling back to the local copy if the R2 fetch fails.
    Pass r2_first=False for local-first — use the local copy when present, fetch from R2
    only on miss (the calibration builder's staging). Cached on disk thereafter.

    NP_R2_DISABLE=1: never reach R2 — return local if present, else raise (fail-closed)."""
    p = Path(local_path)
    if _r2_first_active(r2_first):
        cached = _table_cache() / Path(r2_key).name
        try:
            if not cached.exists():
                r2_copy(r2_key, cached)
            return cached                        # R2 as source of truth
        except Exception:
            if p.exists():
                return p                         # fall back to local on R2 failure
            raise
    if p.exists():
        return p                                 # local-first hit (or fail-closed local)
    if _r2_disabled():
        raise FileNotFoundError(
            f"{local_path} absent and NP_R2_DISABLE=1 (calibration fail-closed — R2 fetch refused)")
    cached = _table_cache() / Path(r2_key).name
    if not cached.exists():
        r2_copy(r2_key, cached)
    return cached


def _check_rclone() -> None:
    """Raise RuntimeError if rclone binary is absent or the r2: remote is unconfigured."""
    try:
        result = subprocess.run(
            ["rclone", "version"],
            capture_output=True, text=True, timeout=10,
        )
    except FileNotFoundError:
        raise RuntimeError(
            f"rclone not found — {_SETUP_HINT}"
        )
    if result.returncode != 0:
        raise RuntimeError(
            f"rclone version failed ({result.returncode}) — {_SETUP_HINT}"
        )


def r2_exists(r2_key: str) -> bool:
    """Return True if r2_key exists in R2.

    Uses `rclone lsf --max-depth 1` which is fast (no data transfer).
    Raises RuntimeError if rclone or the remote is missing.
    """
    if _public():
        return _rel(r2_key) in _public_index()
    r2_key = _canon(r2_key)
    _check_rclone()
    # lsf returns the filename on stdout; empty → not found; non-zero → error
    parent = r2_key.rsplit("/", 1)[0]
    filename = r2_key.rsplit("/", 1)[-1]
    result = subprocess.run(
        ["rclone", "lsf", "--max-depth", "1", parent],
        capture_output=True, text=True, timeout=60,
    )
    if result.returncode != 0:
        err = result.stderr.strip()
        if "didn't find" in err or "No such file" in err or "directory not found" in err.lower():
            return False
        raise RuntimeError(
            f"rclone lsf {parent} failed (rc={result.returncode}): {err} — {_SETUP_HINT}"
        )
    return filename in result.stdout.splitlines()


def r2_copy(r2_key: str, local_dest: str | Path) -> None:
    """Download r2_key → local_dest using `rclone copy`.

    Creates parent directories as needed.
    Raises RuntimeError on rclone failure.
    """
    local_dest = Path(local_dest)
    local_dest.parent.mkdir(parents=True, exist_ok=True)
    if _public():
        _public_download(r2_key, local_dest.parent / Path(_rel(r2_key)).name)
        return
    r2_key = _canon(r2_key)
    _check_rclone()
    # rclone copy <src_file> <dest_dir> — it copies the file into the directory
    result = subprocess.run(
        ["rclone", "copy", "--transfers", "1", r2_key, str(local_dest.parent)],
        capture_output=True, text=True, timeout=3600,  # 1h per file max
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"rclone copy {r2_key} → {local_dest.parent} failed "
            f"(rc={result.returncode}): {result.stderr.strip()} — {_SETUP_HINT}"
        )


def _public_download(r2_key: str, dest: Path) -> None:
    """Download one file from the public mirror (atomic: temp file, then rename)."""
    import shutil
    import urllib.request
    url = _public_url(r2_key, http=True)
    tmp = dest.with_name(f"{dest.name}.{os.getpid()}.part")
    try:
        with urllib.request.urlopen(url, timeout=600) as r, open(tmp, "wb") as f:
            shutil.copyfileobj(r, f, length=1 << 20)
        tmp.replace(dest)
    except Exception as e:
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"download {url} failed: {e}") from e


# ── year enumeration from R2 (clean-machine support) ──────────────────────────
# Year-loop tools historically derived their year list from a LOCAL glob, so on a
# machine with no local mirror they found zero years. list_v1_years() enumerates the
# years a panel family has IN R2 (the source of truth) so callers can union it with
# their local glob — giving the full canonical span on any machine, while staying
# local-only in the calibration jail (the no-fraudlib shim returns []).

@functools.lru_cache(maxsize=1)
def _v1_listing() -> tuple[str, ...]:
    """Filenames in R2 processed/v1 — one `rclone lsf` per process (cached). Returns
    () on any failure, so callers fall back to the local glob."""
    if _public():
        pre = _rel(R2_PANELS) + "/"
        return tuple(sorted(f[len(pre):] for f in _public_index()
                            if f.startswith(pre) and "/" not in f[len(pre):]))
    try:
        _check_rclone()
        result = subprocess.run(
            ["rclone", "lsf", "--files-only", R2_PANELS],
            capture_output=True, text=True, timeout=60,
        )
        if result.returncode == 0:
            return tuple(result.stdout.split())
    except Exception:
        pass
    return ()


def list_v1_years(family: str) -> list[int]:
    """Years for which `<family>_<year>.parquet` exists in R2 processed/v1
    (e.g. 'cms_partb' -> [2013, ..., 2024]). Returns [] when R2 is disabled
    (calibration fail-closed) or not configured, so a caller unions this with its
    local glob: full canonical span on a clean machine, local-only in the jail."""
    if _r2_disabled() or not _r2_configured():
        return []
    suffix = ".parquet"
    prefix = family + "_"
    out: set[int] = set()
    for name in _v1_listing():
        if name.startswith(prefix) and name.endswith(suffix):
            mid = name[len(prefix):-len(suffix)]
            if mid.isdigit():  # excludes e.g. cms_partb_svc_2019 when family='cms_partb'
                out.add(int(mid))
    return sorted(out)


# ── local path -> local file or remote copy ───────────────────────────────────
# Tools written against local paths (data/processed/x.parquet, data/registries/...,
# data/raw/medicaid/...) can wrap them in DataPath: the string form is the local file when
# present, else the remote copy (private bucket or public mirror), so the same SQL runs on
# a fully local machine, a private-bucket machine, or a clean clone reading NP_DATA_URL.
_REPO = Path(__file__).resolve().parents[1]
_LOCAL_TO_BUCKET = (
    ("data/processed/", "processed/v1/"),
    ("data/panels/", "processed/v1/"),
    ("data/registries/", "processed/v1/"),
    ("data/raw/medicaid/", "raw/medicaid/"),
    ("data/ground_truth/", "ground_truth/v1/"),
    ("data/cache/feature_frames/", "cache/v1/feature_frames/"),
)


@functools.lru_cache(maxsize=64)
def _remote_names(rel_dir: str) -> tuple[str, ...]:
    """File names directly under one bucket directory (public index or rclone listing)."""
    if _public():
        pre = rel_dir.rstrip("/") + "/"
        return tuple(f[len(pre):] for f in _public_index()
                     if f.startswith(pre) and "/" not in f[len(pre):])
    if rel_dir.rstrip("/") == _rel(R2_PANELS):
        return _v1_listing()
    try:
        _check_rclone()
        r = subprocess.run(["rclone", "lsf", "--files-only", f"{R2_REMOTE}/{rel_dir}"],
                           capture_output=True, text=True, timeout=60)
        return tuple(r.stdout.split()) if r.returncode == 0 else ()
    except Exception:
        return ()


# Files that queries scan end to end. On the public mirror, thousands of range reads get
# rate-limited (HTTP 429), so locate() downloads these once to their local path instead.
_FETCH_ONCE = frozenset({"raw/medicaid/provider_spending_full.parquet"})


def locate(local_path: str | Path) -> str | None:
    """The DuckDB source for a local data path: the local file (or local glob matches) if
    present, else the remote copy, else None. Never remote when NP_R2_DISABLE=1."""
    import fnmatch
    p = Path(local_path)
    is_glob = any(c in p.name for c in "*?[")
    if (any(p.parent.glob(p.name)) if is_glob else p.exists()):
        return str(p)
    if _r2_disabled() or not _r2_configured():
        return None
    try:
        rel = p.resolve().relative_to(_REPO).as_posix()
    except ValueError:
        return None
    for local_pre, bucket_pre in _LOCAL_TO_BUCKET:
        if rel.startswith(local_pre):
            bucket_rel = bucket_pre + rel[len(local_pre):]
            bucket_dir, name = bucket_rel.rsplit("/", 1)
            names = _remote_names(bucket_dir)
            if (fnmatch.filter(names, name) if is_glob else name in names):
                key = f"{_DEFAULT_PREFIX}/{bucket_rel}"
                if _public() and bucket_rel in _FETCH_ONCE:
                    try:
                        print(f"downloading {name} once from the public dataset "
                              "(several GB; later reads are local)...", file=sys.stderr)
                        p.parent.mkdir(parents=True, exist_ok=True)
                        _public_download(key, p)
                        return str(p)
                    except Exception as e:
                        print(f"warning: {e}; reading it remotely instead", file=sys.stderr)
                return _s3_url(key)
            return None
    return None


def remote_glob(pattern: str) -> list[str]:
    """DuckDB sources for bucket paths matching a glob whose wildcards may sit in any
    segment, e.g. 'results/current/walk_forward_*_y2023_*/scored_full_2023.parquet'.
    [] when no remote source is configured (or NP_R2_DISABLE=1)."""
    import fnmatch
    if _r2_disabled() or not _r2_configured():
        return []
    if _public():
        names = list(_public_index())
    else:
        top = pattern.split("*", 1)[0].rsplit("/", 1)[0]
        try:
            _check_rclone()
            r = subprocess.run(["rclone", "lsf", "-R", "--files-only", f"{R2_REMOTE}/{top}"],
                               capture_output=True, text=True, timeout=120)
            names = [f"{top}/{n}" for n in r.stdout.split()] if r.returncode == 0 else []
        except Exception:
            names = []
    return [_s3_url(f"{_DEFAULT_PREFIX}/{n}") for n in sorted(fnmatch.filter(names, pattern))]


class DataPath:
    """A data path whose str() is a DuckDB-readable source (see locate)."""

    def __init__(self, local: str | Path):
        self.local = Path(local)

    def __truediv__(self, name: str) -> "DataPath":
        return DataPath(self.local / name)

    @property
    def name(self) -> str:
        return self.local.name

    def exists(self) -> bool:
        return locate(self.local) is not None

    def __str__(self) -> str:
        return locate(self.local) or str(self.local)

    def __repr__(self) -> str:
        return f"DataPath({self.local})"
