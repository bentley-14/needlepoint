"""Public read-only mirror (NP_DATA_URL): a clean clone with no bucket credentials reads the
same bucket layout from a public URL. Exercised here against a file:// mirror."""
import json

import duckdb
import pandas as pd
import pytest

from fraudlib import r2


@pytest.fixture()
def mirror(tmp_path, monkeypatch):
    root = tmp_path / "mirror"
    (root / "processed/v1").mkdir(parents=True)
    (root / "raw/medicaid").mkdir(parents=True)
    (root / "ground_truth/v1").mkdir(parents=True)
    pd.DataFrame({"Rndrng_NPI": [1900000007], "Tot_Mdcr_Pymt_Amt": [123.0]}).to_parquet(
        root / "processed/v1/cms_partb_2099.parquet")
    pd.DataFrame({"x": [1]}).to_parquet(root / "raw/medicaid/provider_spending_full.parquet")
    (root / "ground_truth/v1/doctrine_corpus.jsonl").write_text('{"id": "d1"}\n')
    files = sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file())
    (root / "_index.json").write_text(json.dumps({"files": files}))

    monkeypatch.setattr(r2, "DATA_URL", f"file://{root}")
    monkeypatch.setattr(r2, "_rclone_configured", lambda: False)
    monkeypatch.delenv("NP_R2_DISABLE", raising=False)
    for f in (r2._public_index, r2._v1_listing, r2._remote_names, r2.duckdb_r2):
        f.cache_clear()
    yield root
    for f in (r2._public_index, r2._v1_listing, r2._remote_names, r2.duckdb_r2):
        f.cache_clear()


def test_public_mode_lists_and_enumerates_years(mirror):
    assert r2._public() and r2._r2_configured()
    assert "cms_partb_2099.parquet" in r2._v1_listing()
    assert r2.list_v1_years("cms_partb") == [2099]
    assert r2.r2_exists("r2:needlepoint-data/processed/v1/cms_partb_2099.parquet")
    assert not r2.r2_exists("r2:needlepoint-data/processed/v1/cms_partb_1999.parquet")


def test_locate_maps_local_paths_onto_the_mirror(mirror, monkeypatch):
    monkeypatch.setattr(r2, "_FETCH_ONCE", frozenset())   # no download into the real repo
    src = r2.locate(r2._REPO / "data/processed/cms_partb_2099.parquet")
    assert src == f"{mirror}/processed/v1/cms_partb_2099.parquet"
    assert r2.locate(r2._REPO / "data/raw/medicaid/provider_spending_full.parquet")
    assert r2.locate(r2._REPO / "data/processed/cms_partb_1999.parquet") is None
    assert r2.locate(r2._REPO / "data/processed/cms_partb_*.parquet").endswith("cms_partb_*.parquet")


def test_fetch_once_files_download_to_their_local_path(mirror, tmp_path, monkeypatch):
    monkeypatch.setattr(r2, "_REPO", tmp_path / "repo")
    local = tmp_path / "repo/data/raw/medicaid/provider_spending_full.parquet"
    assert r2.locate(local) == str(local)
    assert local.exists() and not list(local.parent.glob("*.part"))


def test_remote_glob_matches_wildcards_in_directories(mirror):
    run = mirror / "results/current/walk_forward_v4_top5_y2099_x"
    run.mkdir(parents=True)
    pd.DataFrame({"npi": ["1900000007"]}).to_parquet(run / "scored_full_2099.parquet")
    files = sorted(p.relative_to(mirror).as_posix() for p in mirror.rglob("*") if p.is_file())
    (mirror / "_index.json").write_text(json.dumps({"files": files}))
    r2._public_index.cache_clear()
    got = r2.remote_glob("results/current/walk_forward_v4_top5_y2099_*/scored_full_2099.parquet")
    assert got == [f"{run}/scored_full_2099.parquet"]
    assert r2.remote_glob("results/current/*/scored_full_1999.parquet") == []


def test_datapath_reads_through_duckdb(mirror):
    p = r2.DataPath(r2._REPO / "data/processed") / "cms_partb_2099.parquet"
    assert p.exists()
    got = r2.duckdb_r2().execute(f"SELECT Tot_Mdcr_Pymt_Amt FROM read_parquet('{p}')").fetchone()
    assert got == (123.0,)


def test_fetch_local_downloads_from_mirror(mirror, tmp_path, monkeypatch):
    monkeypatch.setenv("NP_R2_TABLE_CACHE", str(tmp_path / "cache"))
    got = r2.fetch_local(tmp_path / "absent.jsonl", "r2:needlepoint-data/ground_truth/v1/doctrine_corpus.jsonl")
    assert got.read_text() == '{"id": "d1"}\n'


def test_local_file_wins_over_mirror(mirror, tmp_path):
    local = tmp_path / "cms_partb_2099.parquet"
    local.write_bytes(b"x")
    key = "r2:needlepoint-data/processed/v1/cms_partb_2099.parquet"
    assert r2.parquet_source(local, key) == str(local)
    assert r2.parquet_source(tmp_path / "absent.parquet", key).endswith("processed/v1/cms_partb_2099.parquet")


def test_disable_switch_blocks_the_mirror(mirror, monkeypatch):
    monkeypatch.setenv("NP_R2_DISABLE", "1")
    assert r2.locate(r2._REPO / "data/processed/cms_partb_2099.parquet") is None
    assert r2.list_v1_years("cms_partb") == []


def test_private_bucket_wins_over_mirror(mirror, monkeypatch):
    monkeypatch.setattr(r2, "_rclone_configured", lambda: True)
    assert not r2._public()
    assert r2._s3_url("r2:needlepoint-data/processed/v1/x.parquet").startswith("s3://")


def test_huggingface_urls(monkeypatch):
    monkeypatch.setattr(r2, "DATA_URL", "hf://datasets/someone/needlepoint-data")
    key = "r2:needlepoint-data/processed/v1/leie.parquet"
    assert r2._public_url(key) == "hf://datasets/someone/needlepoint-data/processed/v1/leie.parquet"
    assert r2._public_url(key, http=True) == (
        "https://huggingface.co/datasets/someone/needlepoint-data/resolve/main/processed/v1/leie.parquet")


def test_no_remote_at_all_stays_local(monkeypatch, tmp_path):
    monkeypatch.setattr(r2, "DATA_URL", "")
    monkeypatch.setattr(r2, "_rclone_configured", lambda: False)
    assert not r2._r2_configured()
    assert r2.locate(tmp_path / "nope.parquet") is None
    assert str(r2.DataPath(tmp_path / "nope.parquet")) == str(tmp_path / "nope.parquet")
