"""Tests for pipeline.base — schema validation, write contract, manifests."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd
import pytest
from pandera.typing import Series

from pipeline.base import (
    BaseScraper,
    DocumentSchema,
    ProviderRecordSchema,
    RunResult,
    SchemaViolation,
    url_to_doc_id,
)


# ---------------------------------------------------------------------------
# Test fixtures — toy schemas + scrapers
# ---------------------------------------------------------------------------


class _ToyPartBSchema(ProviderRecordSchema):
    """Minimal Part B–shaped schema for tests."""
    tot_benes: Series[int]
    tot_mdcr_pymt_amt: Series[float]


class _ToyDocSchema(DocumentSchema):
    """Minimal document-shaped schema for tests."""
    title: Series[str]
    body_text: Series[str]


class _ToyPartBScraper(BaseScraper):
    source = "toy_partb"
    schema = _ToyPartBSchema
    schema_version = "v1.0"

    def __init__(self, output_dir: Path, df: pd.DataFrame) -> None:
        self.output_dir = output_dir
        self._df = df

    def fetch(self, *, year: int) -> RunResult:
        started = self._now()
        df = self._df.copy()
        df["data_year"] = year
        df = self._stamp_base_columns(df)
        return self._write(
            df,
            name=f"toy_partb_{year}",
            started_at_utc=started,
            inputs={"url": "https://example.test/toy"},
        )


# ---------------------------------------------------------------------------
# Schema introspection
# ---------------------------------------------------------------------------


def test_provider_record_schema_inherits_base_columns() -> None:
    cols = set(_ToyPartBSchema.to_schema().columns.keys())
    # base columns
    assert {"source", "fetched_at_utc", "schema_version"} <= cols
    # provider-record columns
    assert {"npi", "provider_name", "state", "data_year"} <= cols
    # subclass-specific
    assert {"tot_benes", "tot_mdcr_pymt_amt"} <= cols


def test_document_schema_inherits_base_columns() -> None:
    cols = set(_ToyDocSchema.to_schema().columns.keys())
    assert {"source", "fetched_at_utc", "schema_version"} <= cols
    assert {"url", "doc_id", "publisher", "published_date"} <= cols
    assert {"title", "body_text"} <= cols


def test_expected_columns_on_scraper() -> None:
    expected = _ToyPartBScraper.expected_columns()
    assert "npi" in expected
    assert "tot_benes" in expected
    assert "source" in expected


# ---------------------------------------------------------------------------
# Happy path: validate + write + manifest
# ---------------------------------------------------------------------------


def _well_formed_partb_df() -> pd.DataFrame:
    """A DataFrame whose columns exactly match _ToyPartBSchema (minus
    data_year, which the scraper stamps; and minus the 3 base columns
    which _stamp_base_columns stamps)."""
    return pd.DataFrame(
        {
            "npi": ["1234567890", "1234567891"],
            "provider_name": ["A", "B"],
            "state": ["CA", "NY"],
            "tot_benes": [100, 200],
            "tot_mdcr_pymt_amt": [10000.0, 20000.0],
        }
    )


def test_fetch_writes_parquet_and_manifest(tmp_path: Path) -> None:
    scraper = _ToyPartBScraper(output_dir=tmp_path, df=_well_formed_partb_df())
    result = scraper.fetch(year=2023)

    parquet_path = tmp_path / "toy_partb_2023.parquet"
    manifest_path = tmp_path / "toy_partb_2023.manifest.json"
    assert parquet_path.exists()
    assert manifest_path.exists()

    # parquet round-trip
    df = pd.read_parquet(parquet_path)
    assert len(df) == 2
    assert set(df.columns) == _ToyPartBScraper.expected_columns()
    assert (df["source"] == "toy_partb").all()
    assert (df["schema_version"] == "v1.0").all()
    assert (df["data_year"] == 2023).all()

    # manifest sanity
    manifest = json.loads(manifest_path.read_text())
    assert manifest["source"] == "toy_partb"
    assert manifest["rows"] == 2
    assert manifest["schema_class"] == "_ToyPartBSchema"
    assert manifest["schema_version"] == "v1.0"
    assert set(manifest["schema_columns"]) == _ToyPartBScraper.expected_columns()
    assert manifest["inputs"] == {"url": "https://example.test/toy"}

    # RunResult shape
    assert isinstance(result, RunResult)
    assert result.rows == 2
    assert result.artifact_path == parquet_path


# ---------------------------------------------------------------------------
# Strict validation: missing + unknown columns both raise
# ---------------------------------------------------------------------------


class _MissingColumnScraper(_ToyPartBScraper):
    def fetch(self, *, year: int) -> RunResult:
        started = self._now()
        df = self._df.drop(columns=["tot_benes"])  # missing required col
        df["data_year"] = year
        df = self._stamp_base_columns(df)
        return self._write(df, name=f"toy_{year}", started_at_utc=started)


class _UnknownColumnScraper(_ToyPartBScraper):
    def fetch(self, *, year: int) -> RunResult:
        started = self._now()
        df = self._df.copy()
        df["surprise"] = "wat"                     # column not on schema
        df["data_year"] = year
        df = self._stamp_base_columns(df)
        return self._write(df, name=f"toy_{year}", started_at_utc=started)


def test_missing_required_column_raises(tmp_path: Path) -> None:
    scraper = _MissingColumnScraper(output_dir=tmp_path, df=_well_formed_partb_df())
    with pytest.raises(SchemaViolation) as exc:
        scraper.fetch(year=2023)
    assert "missing required columns" in str(exc.value)
    assert "tot_benes" in str(exc.value)

    # parquet must NOT have been written
    assert not (tmp_path / "toy_2023.parquet").exists()


def test_unknown_column_raises(tmp_path: Path) -> None:
    scraper = _UnknownColumnScraper(output_dir=tmp_path, df=_well_formed_partb_df())
    with pytest.raises(SchemaViolation) as exc:
        scraper.fetch(year=2023)
    assert "unknown columns" in str(exc.value)
    assert "surprise" in str(exc.value)


def test_missing_and_unknown_both_reported(tmp_path: Path) -> None:
    class _Both(_ToyPartBScraper):
        def fetch(self, *, year: int) -> RunResult:
            started = self._now()
            df = self._df.drop(columns=["tot_benes"]).assign(surprise="x")
            df["data_year"] = year
            df = self._stamp_base_columns(df)
            return self._write(df, name=f"toy_{year}", started_at_utc=started)

    scraper = _Both(output_dir=tmp_path, df=_well_formed_partb_df())
    with pytest.raises(SchemaViolation) as exc:
        scraper.fetch(year=2023)
    msg = str(exc.value)
    assert "missing required columns" in msg
    assert "unknown columns" in msg


# ---------------------------------------------------------------------------
# stamp_base_columns idempotency
# ---------------------------------------------------------------------------


def test_stamp_base_columns_adds_all_three(tmp_path: Path) -> None:
    scraper = _ToyPartBScraper(output_dir=tmp_path, df=_well_formed_partb_df())
    df = _well_formed_partb_df()
    stamped = scraper._stamp_base_columns(df)
    assert "source" in stamped.columns
    assert "fetched_at_utc" in stamped.columns
    assert "schema_version" in stamped.columns
    # doesn't mutate caller's frame
    assert "source" not in df.columns


# ---------------------------------------------------------------------------
# url_to_doc_id
# ---------------------------------------------------------------------------


def test_url_to_doc_id_is_deterministic_and_short() -> None:
    a = url_to_doc_id("https://www.justice.gov/opa/pr/example-case")
    b = url_to_doc_id("https://www.justice.gov/opa/pr/example-case")
    c = url_to_doc_id("https://www.justice.gov/opa/pr/different-case")
    assert a == b
    assert a != c
    assert len(a) == 16
    assert all(ch in "0123456789abcdef" for ch in a)


# ---------------------------------------------------------------------------
# transform() hook: default no-op + override for post-parse processing
# ---------------------------------------------------------------------------


def test_default_transform_is_noop(tmp_path: Path) -> None:
    """Plain scrapers that never call self.transform() are unaffected."""
    scraper = _ToyPartBScraper(output_dir=tmp_path, df=_well_formed_partb_df())
    df = _well_formed_partb_df()
    assert scraper.transform(df) is df  # identity — returns the same frame


class _TransformingScraper(_ToyPartBScraper):
    """A scraper whose transform() does real post-parse work.

    Stand-in for the DOJ pattern: the parsed frame is incomplete, and the
    transform step (agent call + deterministic post-processing) fills in
    the missing columns before validation.
    """

    def fetch(self, *, year: int) -> RunResult:
        started = self._now()
        # Pretend the raw "parse" only yields the NPI column.
        df = pd.DataFrame({"npi": ["1234567890", "1234567891"]})
        df = self.transform(df, year=year)
        df = self._stamp_base_columns(df)
        return self._write(df, name=f"toy_{year}", started_at_utc=started)

    def transform(self, df: pd.DataFrame, **kwargs: Any) -> pd.DataFrame:
        out = df.copy()
        out["provider_name"] = ["A", "B"]
        out["state"] = ["CA", "NY"]
        out["data_year"] = kwargs["year"]
        out["tot_benes"] = [100, 200]
        out["tot_mdcr_pymt_amt"] = [10000.0, 20000.0]
        return out


def test_transform_override_runs_between_parse_and_validate(tmp_path: Path) -> None:
    scraper = _TransformingScraper(output_dir=tmp_path, df=_well_formed_partb_df())
    result = scraper.fetch(year=2024)
    df = pd.read_parquet(result.artifact_path)
    # The transform supplied every non-base column
    assert set(df.columns) == _TransformingScraper.expected_columns()
    assert (df["data_year"] == 2024).all()
    assert df["tot_benes"].tolist() == [100, 200]


# ---------------------------------------------------------------------------
# JSONL output format
# ---------------------------------------------------------------------------


class _JsonlScraper(_ToyPartBScraper):
    """Tests the ``output_format = 'jsonl'`` write path."""
    output_format = "jsonl"


def test_jsonl_output_writes_jsonl_with_sibling_manifest(tmp_path: Path) -> None:
    scraper = _JsonlScraper(output_dir=tmp_path, df=_well_formed_partb_df())
    result = scraper.fetch(year=2023)

    jsonl_path = tmp_path / "toy_partb_2023.jsonl"
    manifest_path = tmp_path / "toy_partb_2023.manifest.json"
    assert jsonl_path.exists()
    assert manifest_path.exists()
    # No spurious parquet
    assert not (tmp_path / "toy_partb_2023.parquet").exists()

    # One JSON object per line, parseable as records
    lines = jsonl_path.read_text().strip().splitlines()
    assert len(lines) == 2
    rows = [json.loads(ln) for ln in lines]
    assert rows[0]["npi"] == "1234567890"
    assert rows[0]["source"] == "toy_partb"
    assert rows[0]["schema_version"] == "v1.0"

    # Manifest still reports the actual artifact (jsonl, not parquet)
    manifest = json.loads(manifest_path.read_text())
    assert manifest["artifact_path"].endswith(".jsonl")
    assert manifest["rows"] == 2
