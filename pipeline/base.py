"""Base classes for pipeline producers.

Every producer in ``pipeline/`` is a **scraper** — fetches from an
external source (CMS, DOJ press releases, NPPES, policy PDFs), optionally
post-processes the parsed frame via :meth:`BaseScraper.transform`, and
writes the result to local disk.

Every scraper subclass must:

1. Declare a ``schema`` (a dataclass inheriting from one of the schema
   subclasses below). The schema enumerates *every* column the produced
   artifact will have. Unknown columns raise :class:`SchemaViolation` at
   write-time.
2. Declare an ``output_dir`` for where artifacts land
   (e.g. ``data/panels/`` or ``data/source/``).
3. Implement ``fetch(**kwargs) -> RunResult``. The implementation does
   whatever URL/API dance is needed and returns a populated DataFrame
   passed through ``self._write(...)``.

Most scrapers' ``fetch`` is the canonical download → parse → align →
stamp → write flow. Sources that need post-fetch processing
(e.g. DOJ, where the corpus_curator agent + deterministic NPI resolution
turn raw press releases into ``gold_set.jsonl``) override
:meth:`BaseScraper.transform` to slot that work in between parse and
write. The default ``transform`` is a no-op, so plain scrapers never
have to think about it.

The output contract is uniform across all sources: one artifact (parquet
or JSONL, controlled by ``OUTPUT_FORMAT``) plus a sibling
``.manifest.json`` describing what was produced, when, from where, and
which schema version.

R2 / remote storage
-------------------
Nothing in this module touches R2. Scrapers write to the local
``data/`` tree only. ``make push-data`` (rclone, shell — not Python)
syncs ``data/panels/`` and ``data/source/`` to the team's R2 bucket;
``make pull-data`` mirrors them back. The Python producer and the R2
transport are intentionally decoupled — rclone is good at parallel /
resumable bulk transfer, and we don't want to reinvent it. If you ever
need single-key R2 access from Python (e.g. for an agent-driven
producer that pushes one new file immediately), add a thin
``pipeline.storage`` module rather than baking S3 calls into this ABC.
"""
from __future__ import annotations

import hashlib
import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any, ClassVar, Literal, Optional, Type

import pandas as pd
import pandera.pandas as pa
from pandera.errors import SchemaError, SchemaErrors
from pandera.typing import DateTime, Series


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class SchemaViolation(Exception):
    """Raised when a scraper's DataFrame does not match its declared schema.

    Wraps Pandera's ``SchemaError`` / ``SchemaErrors`` so callers keep
    catching one exception type regardless of the validation engine.
    """


# ---------------------------------------------------------------------------
# Schema hierarchy
# ---------------------------------------------------------------------------
#
# Schemas are Pandera ``DataFrameModel`` classes. The class enumerates the
# columns the produced parquet will have, AND each column's dtype +
# nullability + value checks. Subclasses *add* columns, never remove.
#
# Validation (``BaseScraper._validate``) does three things in one pass:
#   1. coerce  — cast each column to its declared dtype (``Config.coerce``).
#      A column declared ``str`` that arrives as ``float`` (the Open
#      Payments NPI-as-DOUBLE bug) is cast, not silently shipped.
#   2. strict  — reject any column not declared on the schema
#      (``Config.strict``); catches CMS adding a column we didn't model.
#   3. checks  — enforce nullability (``nullable=``) and value checks
#      (``str_matches``, ``ge``/``le``, …) per column.
#
# WHY Pandera and not Pydantic: Pandera validates *columnar* (vectorized
# over whole columns), so it scales to the million-row panels. Pydantic
# would instantiate one model object per row — far too slow here.
#
# NOTE on the ``.0`` trap: coercion is ``astype``. A DOUBLE coerced to
# ``str`` yields the literal ``"1900000075.0"``. That's why NPI columns
# also carry a ``str_matches(r"^(\d{10})?$")`` check — so a float that
# slipped through upstream FAILS loudly at write-time instead of shipping
# garbage to R2. Pandera enforces the contract; it does not repair a bad
# upstream cast. The producer must still build ``npi`` as a clean string.

# tz-aware UTC timestamp dtype for the machine-written ``fetched_at_utc``
# stamp. Pandera needs the timezone declared explicitly via Annotated.
_UTC_TIMESTAMP = Annotated[pd.DatetimeTZDtype, "ns", "UTC"]


class BaseScraperSchema(pa.DataFrameModel):
    """Columns required on every parquet a scraper produces.

    Inherit from a more specific subclass (e.g. :class:`ProviderRecordSchema`
    or :class:`DocumentSchema`) rather than from this directly.

    When to bump ``schema_version``
    ------------------------------
    Each scraper class declares its own ``schema_version`` string (e.g.
    ``"v1.0"``) which gets stamped onto every row + into the manifest.
    Bump it whenever the produced parquet's *shape or meaning* changes,
    using semver:

    * **patch** (``v1.0`` → ``v1.0.1``) — bug fix that doesn't change the
      schema or value semantics (e.g. fix a parse error that was leaving
      a column null).
    * **minor** (``v1.0`` → ``v1.1``) — add a new column. Backward
      compatible: consumers that don't know about the column still work.
    * **major** (``v1.0`` → ``v2.0``) — remove a column, rename a column,
      change a column's dtype, or change a column's *semantics* (e.g.
      switching ``payment_amt`` from "submitted" to "allowed"). All
      cause silent failures downstream if not flagged.

    Don't bump for upstream cosmetic changes (CMS publishes the same
    column under a slightly different label) — those go in the parser,
    not the schema, because the parquet's column name doesn't change.
    Bump for changes the *parquet consumer* would notice.

    Strict validation already catches "column appeared/disappeared
    silently" at write-time. ``schema_version`` is for the slower kind
    of drift: cases where the column name is unchanged but the meaning
    quietly shifted.
    """

    source: Series[str]                                  # short identifier, e.g. "cms_partb"
    fetched_at_utc: Series[_UTC_TIMESTAMP] = pa.Field(nullable=True)
    schema_version: Series[str]                          # see "When to bump" above

    class Config:
        coerce = True   # cast each column to its declared dtype
        strict = True   # reject any column not declared on the schema


class ProviderRecordSchema(BaseScraperSchema):
    """Base for NPI-keyed sources: CMS panels, NPPES, PECOS, LEIE, OpenPayments.

    Subclasses add source-specific columns (billing aggregates, taxonomies,
    etc.) on top of these.

    ``npi`` is declared ``str`` with a ``^(\\d{10})?$`` check — exactly 10
    digits, or empty string (LEIE rows may carry no NPI). The check is
    what turns the NPI-as-DOUBLE bug into a write-time failure: a float
    coerces to ``"1900000075.0"``, which the regex rejects.

    ``provider_name`` format convention
    -----------------------------------
    Every NPI-keyed scraper must produce ``provider_name`` strings via
    :func:`format_provider_name` so cross-source joins on display name
    work without case/whitespace mismatches:

    * Individuals (entity code ``I``): ``"LAST, FIRST MI"`` — MI omitted
      when blank, all upper-case, single-space-collapsed.
    * Organizations (entity code ``O``): the org name as-is, upper-cased
      and whitespace-collapsed.
    * Missing or unresolved: ``None`` (never empty string).
    """

    npi: Series[str] = pa.Field(str_matches=r"^(\d{10})?$", nullable=True)
    provider_name: Series[str] = pa.Field(nullable=True)
    state: Series[str] = pa.Field(nullable=True)        # 2-letter abbreviation
    # Calendar year the row's data covers. CMS publishes annual "PUFs"
    # (Public Use Files), so a Part B row with data_year=2023 reflects
    # billing during 2023 even though the file was released in 2025.
    # Leave null for evergreen registries (NPPES current snapshot).
    data_year: Series[int] = pa.Field(ge=2000, le=2100, nullable=True)


class DocumentSchema(BaseScraperSchema):
    """Base for URL-keyed sources: DOJ press releases, OIG reports, policy PDFs.

    Subclasses add body fields, extracted entities, etc.
    """

    url: Series[str]
    doc_id: Series[str]                                  # deterministic hash of url for dedup
    publisher: Series[str]                               # "DOJ", "OIG", "CMS", …
    published_date: Series[DateTime] = pa.Field(nullable=True)


# ---------------------------------------------------------------------------
# Run result (becomes the manifest JSON next to the parquet)
# ---------------------------------------------------------------------------


@dataclass
class RunResult:
    """Summary of one scraper run — serialized to ``<artifact>.manifest.json``."""

    source: str
    artifact_path: Path
    rows: int
    bytes: int
    started_at_utc: datetime
    ended_at_utc: datetime
    schema_class: str                         # e.g. "CMSPartBSchema"
    schema_version: str
    schema_columns: list[str]
    inputs: dict[str, str] = field(default_factory=dict)  # e.g. {"url": "https://cms.gov/...", "sha256": "..."}

    def to_json(self) -> str:
        payload = {
            "source": self.source,
            "artifact_path": str(self.artifact_path),
            "rows": self.rows,
            "bytes": self.bytes,
            "started_at_utc": self.started_at_utc.isoformat(),
            "ended_at_utc": self.ended_at_utc.isoformat(),
            "schema_class": self.schema_class,
            "schema_version": self.schema_version,
            "schema_columns": self.schema_columns,
            "inputs": self.inputs,
        }
        return json.dumps(payload, indent=2, default=str)


# ---------------------------------------------------------------------------
# BaseScraper ABC
# ---------------------------------------------------------------------------


class BaseScraper(ABC):
    """ABC for "scrape one source → write one parquet" producers.

    Subclass contract:

    .. code-block:: python

        class CMSPartBScraper(BaseScraper):
            source = "cms_partb"
            schema = CMSPartBSchema
            output_dir = Path("data/panels")
            schema_version = "v1.0"

            def fetch(self, *, year: int) -> RunResult:
                started = self._now()
                df = self._download_and_parse(year)
                df = self._stamp_base_columns(df)
                return self._write(df, name=f"cms_partb_{year}",
                                   started_at_utc=started,
                                   inputs={"url": ...})
    """

    source: ClassVar[str]
    schema: ClassVar[Type[BaseScraperSchema]]
    output_dir: ClassVar[Path]
    schema_version: ClassVar[str]

    # ``"parquet"`` is the default; ``"jsonl"`` exists for cases where the
    # produced artifact is genuinely line-oriented JSON (the DOJ →
    # gold_set scraper writes ``gold_set.jsonl`` as its canonical form).
    output_format: ClassVar[Literal["parquet", "jsonl"]] = "parquet"

    # ---- the one method subclasses must implement ----

    @abstractmethod
    def fetch(self, **kwargs: Any) -> RunResult:
        """Fetch + parse + validate + write. Returns the run result."""

    # ---- optional hook for post-parse processing ----

    def transform(self, df: pd.DataFrame, **kwargs: Any) -> pd.DataFrame:
        """Hook for post-parse processing. Default: no-op (returns ``df``).

        Most scrapers leave this alone — the parsed CSV/JSON already has
        the shape they want and the canonical column derivation happens
        inline in ``fetch``. Override when the parsed frame needs further
        work before validation, e.g.:

        * **DOJ → gold_set**: ``transform`` calls the corpus_curator
          agent to extract structured cases from raw press-release text,
          then runs the deterministic NPI-resolution + outcome-cap +
          merge passes that produce the canonical ``gold_set.jsonl``
          frame.
        * **A scraper with optional secondary lookups**: ``transform``
          could enrich each row with a side join (e.g. NPPES lookup) if
          the cost makes it impractical to put in ``fetch`` itself.

        ``**kwargs`` mirrors :meth:`fetch`'s shape so the override can
        read e.g. ``year`` without the caller having to pass it twice.
        """
        return df

    # ---- helpers ----

    @staticmethod
    def _now() -> datetime:
        return datetime.now(timezone.utc)

    @classmethod
    def expected_columns(cls) -> set[str]:
        """The full set of columns the schema declares."""
        return set(cls.schema.to_schema().columns.keys())

    @classmethod
    def string_read_dtypes(cls) -> dict[str, str]:
        """Map of schema-declared string columns -> ``"string"``, for
        ``pd.read_csv(dtype=...)``.

        Why pin string columns AT READ rather than relying on Pandera's
        ``coerce``: coercion runs *after* ``read_csv``, on whatever pandas
        already inferred. A zero-padded / alphanumeric code that pandas reads
        as numeric is **already destroyed** by then — ``"06"`` became ``6.0``,
        ``"9C"`` became ``NaN`` — and no later cast (Pandera coerce included)
        can recover it. Telling pandas to read every schema-``str`` column as
        text up front is the only place to prevent that loss; genuinely-numeric
        columns are absent from this map, so pandas infers them and Pandera
        coerces them to their declared dtype exactly as before.

        Columns the scraper derives *after* the read (``npi``, ``provider_name``,
        ``state``) may appear here but aren't in the raw CSV — ``read_csv``
        ignores ``dtype`` keys it doesn't find, so passing them is harmless.
        """
        return {
            name: "string"
            for name, col in cls.schema.to_schema().columns.items()
            if str(col.dtype).startswith(("str", "string", "object"))
        }

    def _validate(self, df: pd.DataFrame) -> pd.DataFrame:
        """Coerce + strict-validate against the schema; return the coerced frame.

        Runs Pandera validation (``lazy=True``, so every violation is
        collected in one pass rather than dying on the first). On success
        returns a NEW frame with each column cast to its declared dtype —
        callers must write *this* frame, not the original, so the
        coercion lands in the artifact.

        On failure raises :class:`SchemaViolation`, translating Pandera's
        report back into the historical message vocabulary:

        * ``missing required columns: [...]`` — a declared column absent
          from the frame (Pandera ``column_in_dataframe``).
        * ``unknown columns not declared on <Schema>: [...]`` — a column
          present but not declared, rejected by ``Config.strict``
          (Pandera ``column_in_schema``).
        * ``dtype/null/value violations — ...`` — coercion failures,
          nulls in a non-nullable column, or failed value checks
          (e.g. an NPI that isn't 10 digits because it arrived as a float).

        The contract is still "the DataFrame's columns equal the schema's
        columns, exactly" (strict over additive, 2026-05-28) so upstream
        schema drift surfaces at the producer instead of leaking into the
        ranker as a silent KeyError — now with dtype + nullability +
        value checks on top.
        """
        try:
            return self.schema.validate(df, lazy=True)
        except (SchemaError, SchemaErrors) as exc:
            raise SchemaViolation(self._format_schema_errors(exc)) from exc

    def _format_schema_errors(self, exc: Exception) -> str:
        """Translate a Pandera error into a legacy-style SchemaViolation message."""
        fc = getattr(exc, "failure_cases", None)
        if fc is None or len(fc) == 0:
            return f"{self.source}: {exc}"

        checks = fc["check"].astype(str)
        missing = sorted(
            fc.loc[checks == "column_in_dataframe", "failure_case"].astype(str).unique()
        )
        unknown = sorted(
            fc.loc[checks == "column_in_schema", "failure_case"].astype(str).unique()
        )
        other = fc.loc[~checks.isin(["column_in_dataframe", "column_in_schema"])]

        problems: list[str] = []
        if missing:
            problems.append(f"missing required columns: {missing}")
        if unknown:
            problems.append(
                f"unknown columns not declared on {self.schema.__name__}: {unknown}"
            )
        if len(other):
            seen = [
                f"{r['column']}: {r['check']} (e.g. {r['failure_case']!r})"
                for _, r in other.head(20).iterrows()
            ]
            problems.append("dtype/null/value violations — " + "; ".join(seen))
        return f"{self.source}: " + "; ".join(problems)

    def _align_to_schema(self, df: pd.DataFrame) -> pd.DataFrame:
        """Add schema-declared columns missing from ``df`` as all-NaN.

        Strict validation rejects both missing *and* unknown columns. The
        "unknown" rule is load-bearing (catches CMS adding a column we
        didn't declare). The "missing" rule is too strict in one specific
        case: cross-year schema drift, where an upstream PUF adds columns
        partway through its history. DMEPOS 2014–2016 has no chronic-
        condition columns; 2017+ does.

        Calling this method *before* :meth:`_validate` opts the scraper
        into "treat declared-but-absent columns as NaN", uniformly
        materializing the canonical schema across years. Unknown columns
        still surface as a validation failure. Use this when older
        snapshots are a subset of the current canonical schema; do not
        use it to paper over a genuine column rename.
        """
        out = df.copy()
        for name in self.expected_columns() - set(out.columns):
            out[name] = pd.NA
        return out

    def _stamp_base_columns(self, df: pd.DataFrame) -> pd.DataFrame:
        """Add the three columns every parquet must carry (``source``,
        ``fetched_at_utc``, ``schema_version``).

        Subclasses call this near the end of ``fetch()`` so they don't
        have to hard-code these on every row themselves.
        """
        df = df.copy()
        df["source"] = self.source
        df["fetched_at_utc"] = self._now()
        df["schema_version"] = self.schema_version
        return df

    def _write(
        self,
        df: pd.DataFrame,
        *,
        name: str,
        started_at_utc: datetime,
        inputs: Optional[dict[str, str]] = None,
    ) -> RunResult:
        """Validate, write the artifact, write the manifest, return result.

        ``name`` is the filename stem (no extension). The artifact lands
        at ``self.output_dir / f"{name}.{ext}"`` where ``ext`` is
        ``parquet`` or ``jsonl`` per :attr:`output_format`. The manifest
        is always sibling JSON at ``self.output_dir / f"{name}.manifest.json"``.
        """
        # Validation coerces dtypes; write the coerced frame, not the input.
        df = self._validate(df)

        self.output_dir.mkdir(parents=True, exist_ok=True)
        if self.output_format == "parquet":
            artifact_path = self.output_dir / f"{name}.parquet"
            df.to_parquet(artifact_path, index=False)
        elif self.output_format == "jsonl":
            artifact_path = self.output_dir / f"{name}.jsonl"
            # ``orient="records", lines=True`` matches the convention used
            # by ``data/extracted/gold_set.jsonl``: one JSON object per line,
            # no enclosing array, easy to ``jq`` over and to append to.
            # ``date_format="iso"`` is load-bearing: a DateTime column (e.g.
            # DocumentSchema.published_date, which pandera coerces to datetime64)
            # would otherwise serialize as epoch-MILLISECOND ints — and a reader
            # expecting an ISO string (the Lead contract) chokes on the int.
            df.to_json(artifact_path, orient="records", lines=True, date_format="iso")
        else:  # pragma: no cover — guarded by Literal at type-check time
            raise ValueError(
                f"{self.source}: unsupported output_format "
                f"{self.output_format!r}"
            )

        ended = self._now()
        result = RunResult(
            source=self.source,
            artifact_path=artifact_path,
            rows=len(df),
            bytes=artifact_path.stat().st_size,
            started_at_utc=started_at_utc,
            ended_at_utc=ended,
            schema_class=self.schema.__name__,
            schema_version=self.schema_version,
            schema_columns=sorted(self.expected_columns()),
            inputs=inputs or {},
        )

        manifest_path = self.output_dir / f"{name}.manifest.json"
        manifest_path.write_text(result.to_json())

        return result


# ---------------------------------------------------------------------------
# Utility: stable doc_id for DocumentSchema-typed scrapers
# ---------------------------------------------------------------------------


def url_to_doc_id(url: str) -> str:
    """Deterministic 16-hex-char doc id derived from URL.

    Stable across runs; suitable as a primary key for press releases /
    policy PDFs where ``DocumentSchema.doc_id`` is required.
    """
    return hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Provider name normalization
# ---------------------------------------------------------------------------


def _norm(value: Any) -> str:
    """Trim, collapse whitespace, upper-case. Returns ``""`` for nulls."""
    if value is None:
        return ""
    s = str(value).strip()
    if not s or s.lower() == "nan":
        return ""
    return " ".join(s.split()).upper()


def format_provider_name(
    *,
    entity_code: Optional[str],
    last_or_org: Any,
    first: Any = None,
    middle: Any = None,
) -> Optional[str]:
    """Canonicalize a provider display name across NPI-keyed sources.

    Individual (``entity_code == "I"``): ``"LAST, FIRST MI"`` — MI dropped
    when blank. Organization (``entity_code == "O"``): the org name only,
    upper-cased.

    Returns ``None`` if the resulting name would be empty — never ``""``.
    Callers pass raw upstream values; this function handles nulls,
    ``"nan"`` strings, and stray whitespace.
    """
    ent = (entity_code or "").strip().upper() if entity_code else ""
    last = _norm(last_or_org)

    if ent == "O":
        return last or None

    fst = _norm(first)
    mid = _norm(middle)

    if not last and not fst:
        return None
    head = last
    tail = " ".join(p for p in (fst, mid) if p)
    if head and tail:
        return f"{head}, {tail}"
    return head or tail or None
