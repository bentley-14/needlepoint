"""The qui-tam filing test — one legible, deterministic decision.

This REPLACES the opaque additive strength-score + EVF-band + scattered floor
ladder. A case files only if it affirmatively clears the legal predicates of an
FCA qui-tam complaint, evaluated on CONFIRMED evidence. The default is DECLINE.

Each predicate is a named, plain-language check with a one-line rationale, so a
human can read *why* a case filed or declined — there is no mystery number. The
predicates map to the actual filing requirements:

  P0 bookkeeping_coherent    — deterministic backstop: refuses FILE when
                               counts_retained is empty or the last adversarial
                               settlement_verdict was NOT_ACTIONABLE, independent
                               of how P1-P6 derive (the CASE-6A378AB5/Spruce bug:
                               FILE_WITH_DISCOVERY_DEPS recorded on top of a
                               zero-retained, NOT_ACTIONABLE adversarial layer)
  P1 confirmed_false_claim   — Rule 9(b): an actual false claim, not a statistic
  P2 innocent_foreclosed     — Twombly/Iqbal: fraud is the MORE-plausible inference
  P3 materiality             — the falsity is material to payment
  P4 scienter                — knowing/reckless, pleaded
  P5 confirmed_damages       — real confirmed loss (magnitude tier is secondary)
  P6 jurisdiction_clear      — not barred by first-to-file / public-disclosure / SOL

FILE requires ALL predicates MET (P2 foreclosed). Within FILE, the EVF (an
economics "is it worth the seal-period investment" signal) only sets the
SETTLEMENT_READY-vs-FILE_WITH_DISCOVERY_DEPS sub-tier — it never substitutes for
the legal merits.

NOTE (phasing): P1 currently asks only "≥1 count has a falsity anchor." Phase 3
sharpens it to require the anchor be confirmed-fact-tier, grounded in AVAILABLE
data (not blocked-pending-discovery), and not defeated by a live innocent
explanation — which is what flips the over-anomalous controls (Laurel/Cypress)
to DECLINE while real-but-small true positives (Aspen, $80K) still file.
The magnitude of damages is deliberately NOT a file/decline gate: a genuine TP
can confirm little in-sandbox; dollars only move the sub-tier.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from . import scoring
from .schemas import FinalVerdict

MET = "MET"
FAILED = "FAILED"
DISCOVERY_DEPENDENT = "DISCOVERY_DEPENDENT"

# A token floor — confirmed loss must be > $0. Magnitude does NOT gate file/decline
# (a real TP may confirm very little before discovery); it only affects the sub-tier.
MIN_CONFIRMED_DAMAGES_USD = 1.0

_DECLINE_VERDICT = FinalVerdict.NOT_ACTIONABLE


@dataclass(frozen=True)
class Predicate:
    name: str
    status: str          # MET | DISCOVERY_DEPENDENT | FAILED
    rationale: str       # plain-language, human-readable


@dataclass(frozen=True)
class VerdictDecision:
    verdict: FinalVerdict
    status: str                       # case status ("complete" | "abandoned")
    predicates: list[Predicate]
    rationale: str                    # one-line summary of the decision

    def is_file(self) -> bool:
        return self.verdict in (FinalVerdict.SETTLEMENT_READY,
                                FinalVerdict.FILE_WITH_DISCOVERY_DEPS)


@dataclass
class Scorecard:
    """Structured legal facts the decision reads — built by the caller from
    agent outputs (live orchestrator state) or a saved summary (replay oracle)."""
    n_counts_with_confirmed_falsity: int = 0   # P1
    fraud_more_plausible: bool | None = None   # P2 (None = foreclosure not assessed)
    materiality_defeated: bool = False         # P3 (surviving dispositive materiality attack)
    scienter_defeated: bool = False            # P4 (surviving dispositive scienter attack)
    jurisdiction_barred: bool = False          # P6 (surviving dispositive PD/FTF/SOL attack)
    confirmed_damages_usd: float = 0.0         # P5
    evf: float = 0.0                           # economics sub-tier ONLY
    damages_discovery_dependent: bool = False  # reported; not a file/decline gate
    calibration: bool = True
    # Deterministic bookkeeping-coherence override (independent of the 6
    # predicates above, which are DERIVED from these same facts and can
    # theoretically mis-derive — see the CASE-6A378AB5/Spruce bug). None means
    # "not tracked" (pre-refactor back-compat: don't force-decline on absence).
    counts_retained: list[str] | None = None       # from CaseState.counts_retained
    last_settlement_verdict: str | None = None     # last adversarial round's settlement_verdict


def _predicates(sc: Scorecard) -> list[Predicate]:
    p: list[Predicate] = []

    # P0 — bookkeeping coherence (deterministic, independent of P1-P6's
    # derivation). Provider Spruce/CASE-6A378AB5 artifact: a FILE_WITH_DISCOVERY_DEPS
    # verdict recorded with all-predicates-MET on top of an adversarial layer
    # that itself recorded settlement_verdict=NOT_ACTIONABLE and
    # counts_abandoned=[C1,C2,C3] (zero retained). P1-P6 are DERIVED from the
    # same underlying facts and can theoretically mis-derive (a schema drift,
    # an unclassified attack_type, ...); this predicate is a deterministic
    # backstop that does not depend on that derivation being correct — it reads
    # counts_retained / the last settlement_verdict directly. None means "not
    # tracked" (pre-refactor back-compat) and never fails this predicate.
    if sc.counts_retained is not None and len(sc.counts_retained) == 0:
        p.append(Predicate("bookkeeping_coherent", FAILED,
                           "counts_retained is empty — no count survived adversarial "
                           "testing, so there is nothing left to file"))
    elif sc.last_settlement_verdict == "NOT_ACTIONABLE":
        p.append(Predicate("bookkeeping_coherent", FAILED,
                           "the last adversarial round's settlement_verdict was "
                           "NOT_ACTIONABLE — a FILE-class verdict cannot sit on top of it"))
    else:
        p.append(Predicate("bookkeeping_coherent", MET,
                           "counts_retained + settlement_verdict are consistent with a FILE posture"))

    # P1 — confirmed false claim (Rule 9(b))
    if sc.n_counts_with_confirmed_falsity >= 1:
        p.append(Predicate("confirmed_false_claim", MET,
                           f"{sc.n_counts_with_confirmed_falsity} count(s) carry a confirmed falsity anchor"))
    else:
        p.append(Predicate("confirmed_false_claim", FAILED,
                           "no count has a confirmed false-claim anchor — only inferred-from-statistics"))

    # P2 — innocent explanation foreclosed (Twombly/Iqbal)
    if sc.fraud_more_plausible is False:
        p.append(Predicate("innocent_foreclosed", FAILED,
                           "a live innocent explanation is at least as plausible as fraud"))
    elif sc.fraud_more_plausible is True:
        p.append(Predicate("innocent_foreclosed", MET,
                           "fraud is the more-plausible inference; innocent explanations do not survive"))
    else:
        p.append(Predicate("innocent_foreclosed", MET,
                           "foreclosure not assessed (no ledger) — not treated as a live explanation"))

    # P3 — materiality
    p.append(Predicate("materiality", FAILED if sc.materiality_defeated else MET,
                       "materiality defeated by a surviving dispositive attack"
                       if sc.materiality_defeated else "falsity is material to payment"))

    # P4 — scienter
    p.append(Predicate("scienter", FAILED if sc.scienter_defeated else MET,
                       "scienter defeated by a surviving dispositive attack"
                       if sc.scienter_defeated else "knowing/reckless conduct pleaded"))

    # P5 — confirmed damages (magnitude is NOT the gate; only that real loss is confirmed)
    if sc.confirmed_damages_usd >= MIN_CONFIRMED_DAMAGES_USD:
        p.append(Predicate("confirmed_damages", MET,
                           f"confirmed loss ${sc.confirmed_damages_usd:,.0f}"
                           + (" (full magnitude is discovery-dependent)" if sc.damages_discovery_dependent else "")))
    else:
        p.append(Predicate("confirmed_damages", FAILED, "no confirmed loss"))

    # P6 — jurisdiction
    p.append(Predicate("jurisdiction_clear", FAILED if sc.jurisdiction_barred else MET,
                       "barred by a surviving first-to-file / public-disclosure / SOL attack"
                       if sc.jurisdiction_barred else "no surviving jurisdictional bar"))
    return p


def compute_verdict(sc: Scorecard) -> VerdictDecision:
    """Sole verdict-setter. Default DECLINE; FILE only when every legal predicate
    is met. EVF sets the FILE sub-tier (SETTLEMENT_READY vs FILE_WITH_DISCOVERY_DEPS)
    AFTER the legal test passes — never as a merits substitute."""
    preds = _predicates(sc)
    failed = [p for p in preds if p.status == FAILED]

    if failed:
        why = "; ".join(f"{p.name}: {p.rationale}" for p in failed)
        return VerdictDecision(_DECLINE_VERDICT, "abandoned", preds,
                               f"DECLINE — failed predicate(s): {why}")

    # All predicates met → FILE-class. EVF (economics) sets ONLY the sub-tier:
    # ≥0.40 settlement-ready now, else file-and-confirm-in-discovery. This is the
    # one place EVF is consulted, and only after the legal test has passed.
    rec = scoring.evf_recommendation(sc.evf)
    if rec == "FILE":
        return VerdictDecision(FinalVerdict.SETTLEMENT_READY, "case_ready", preds,
                               "FILE — all filing predicates met; EVF clears the settlement-ready bar")
    return VerdictDecision(FinalVerdict.FILE_WITH_DISCOVERY_DEPS, "case_ready", preds,
                           "FILE — all filing predicates met; magnitude/economics confirmed in discovery")


_FILE_VERDICTS = {"SETTLEMENT_READY", "FILE_WITH_DISCOVERY_DEPS"}


def render_filing_decision_md(verdict: str, rationale: str | None,
                              predicates: list[dict] | None,
                              evf: float | None = None,
                              strength: float | None = None,
                              bucket: str | None = None) -> str:
    """Render the plain-English filing-decision scorecard for the dossier.

    The legibility payoff of the verdict refactor: a non-engineer reads WHICH
    legal predicate decided the case, with no opaque score. Deterministic — built
    from compute_verdict's persisted predicates, not the LLM case_writer."""
    is_file = verdict in _FILE_VERDICTS
    out = [
        "# Filing Decision",
        "",
        f"**Verdict: {verdict} — {'FILE' if is_file else 'DECLINE'}**",
        "",
        rationale or "_(no rationale recorded)_",
        "",
        "## Legal-predicate filing test (qui tam)",
        "",
        "| Predicate | Status | Why |",
        "|---|---|---|",
    ]
    for p in (predicates or []):
        why = str(p.get("why") or "").replace("|", r"\|")
        out.append(f"| {p.get('name', '?')} | {p.get('status', '?')} | {why} |")
    if not predicates:
        out.append("| _(none)_ | — | case terminated before the verdict stage, or a pre-refactor run |")
    if evf is not None or strength is not None:
        s = f"{strength:.3f}" if strength is not None else "n/a"
        e = f"{evf:.2f}" if evf is not None else "n/a"
        out += ["", f"_Economics (secondary — never a merits substitute): EVF = {e}; "
                f"strength = {s} ({bucket or 'n/a'}), reporting-only._"]
    return "\n".join(out) + "\n"


# ── Scorecard builder ────────────────────────────────────────────────────────
# Assembles the legal-predicate Scorecard from a case's persisted state. The
# input is the CaseState serialized to a dict (== summary.json, since
# write_summary is state.model_dump_json()). Used by BOTH the live orchestrator
# (at finalize, on json.loads(state.model_dump_json())) and the replay oracle
# (on the saved summary.json) — one builder, so the live decision and the oracle
# are identical by construction.

# attack types whose DISPOSITIVE_NOT_SURVIVED disposition fails a predicate
_MATERIALITY = {"materiality_lacking"}
_SCIENTER = {"scienter_lacking"}
_JURISDICTION = {"public_disclosure_bar", "first_to_file_bar", "statute_of_limitations"}

# Text backstop: a DISPOSITIVE_NOT_SURVIVED attack whose attack_type the legal_adv
# agent left null/uncanonical must STILL register as a jurisdiction bar if its
# reasoning is plainly a first-to-file / public-disclosure / SOL argument. Without
# this, a real dispositive bar leaks through as P6=MET → a false FILE that
# contradicts the case's own economics (the Spruce/Warren first-to-file case:
# disposition=DISPOSITIVE_NOT_SURVIVED but attack_type=None → jurisdiction_barred
# stayed False → verdict FILE while the settlement reasoning said "barred / $0").
_FTF_TEXT = re.compile(r"first[\s-]?to[\s-]?file|3730\s*\(?\s*b\s*\)?\s*\(?\s*5\s*\)?|\bKBR\b|\bCarter\b|Branch\s+Consultants", re.I)
_PD_TEXT = re.compile(r"public[\s-]?disclosure|3730\s*\(?\s*e\s*\)?\s*\(?\s*4\s*\)?", re.I)
_SOL_TEXT = re.compile(r"statute\s+of\s+limitations|\bSOL\b|time[\s-]?barred", re.I)


def _classify_dispositive_attack(a: dict) -> str:
    """attack_type for a DISPOSITIVE_NOT_SURVIVED attack — the agent's classification
    when set, else inferred from the reasoning text so a jurisdiction bar can't leak
    through merely because attack_type was left unset."""
    cl = a.get("classification") or {}
    t = cl.get("attack_type") or a.get("attack_type")
    if t and t != "?":
        return t
    txt = f"{a.get('reasoning') or ''} {cl.get('rationale') or ''} {a.get('argument') or ''}"
    if _FTF_TEXT.search(txt):
        return "first_to_file_bar"
    if _PD_TEXT.search(txt):
        return "public_disclosure_bar"
    if _SOL_TEXT.search(txt):
        return "statute_of_limitations"
    return t or "?"


def _phase_b_build(summary: dict) -> dict:
    """The Phase-B counsel_build (last iter with one; back-compat to last overall)."""
    best: dict = {}
    for it in (summary.get("iterations") or []):
        if it.get("counsel_build"):
            best = it["counsel_build"]
            if it.get("phase") == "rebuttal_closing":
                return it["counsel_build"]
    return best


def _surviving_dispositive_types(summary: dict) -> set[str]:
    out: set[str] = set()
    for r in (summary.get("adversarial_rounds") or []):
        la = (r.get("legal_adv_response") or {}) if isinstance(r, dict) else {}
        for a in (la.get("attack_dispositions") or []):
            if (a.get("disposition") or "") == "DISPOSITIVE_NOT_SURVIVED":
                out.add(_classify_dispositive_attack(a))
    return out


def build_scorecard(summary: dict, calibration: bool = True) -> Scorecard:
    """Build the legal-predicate Scorecard from a serialized CaseState (dict).

    Phase-1 predicate inputs (lenient where Phase-3 will sharpen):
      P1 confirmed_false_claim — # of RETAINED counts with a non-empty falsity
         anchor (Phase 3: require confirmed-tier + available-data + uncontested).
      P5 confirmed_damages — sum of retained counts' damages_floor (Phase 3:
         restrict to confirmed-falsity floors only).
    """
    build = _phase_b_build(summary)
    retained = set(summary.get("counts_retained") or [])
    counts = build.get("counts_pled") or []
    fb = {f.get("count_id"): f for f in (build.get("factual_basis_per_count") or [])}

    def _ret(cid):  # no recorded retained set → treat all as retained
        return (cid in retained) if retained else True

    n_falsity = sum(
        1 for c in counts
        if _ret(c.get("count_id")) and fb.get(c.get("count_id"), {}).get("falsity_anchor_evidence_ids")
    )
    confirmed_damages = sum(
        float(c.get("damages_floor_usd") or 0.0) for c in counts if _ret(c.get("count_id"))
    )
    damages_dep = any(c.get("ceiling_requires") for c in counts if _ret(c.get("count_id")))
    surviving = _surviving_dispositive_types(summary)
    ledger = summary.get("foreclosure_ledger") or {}

    # P0 bookkeeping-coherence inputs — read RAW from summary (not through the
    # lenient _ret() fallback above, which treats an unrecorded set as
    # "everything retained" and would defeat the purpose of this backstop).
    # "counts_retained" key absent entirely -> None (not tracked, pre-refactor
    # back-compat); key present as [] -> genuinely zero retained.
    counts_retained = summary.get("counts_retained") if "counts_retained" in summary else None
    last_settlement_verdict = None
    rounds = summary.get("adversarial_rounds") or []
    if rounds:
        last_lar = (rounds[-1] or {}).get("legal_adv_response") or {}
        last_settlement_verdict = last_lar.get("settlement_verdict")

    return Scorecard(
        n_counts_with_confirmed_falsity=n_falsity,
        fraud_more_plausible=ledger.get("fraud_is_more_plausible_inference"),
        materiality_defeated=bool(surviving & _MATERIALITY),
        scienter_defeated=bool(surviving & _SCIENTER),
        jurisdiction_barred=bool(surviving & _JURISDICTION),
        confirmed_damages_usd=confirmed_damages,
        evf=float(summary.get("expected_value_factor") or 0.0),
        damages_discovery_dependent=damages_dep,
        calibration=calibration,
        counts_retained=counts_retained,
        last_settlement_verdict=last_settlement_verdict,
    )
