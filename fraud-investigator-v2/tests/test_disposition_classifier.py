"""Disposition classifier for the adjudicated true-negative cohort builder.

The KEEP/EXCLUDE filter is load-bearing: a wrong EXCLUDE drops a defensible true
negative; a wrong KEEP smuggles a real-fraud case (settlement/criminal) into the
precision cohort and would make the false-positive firewall look broken. These
pin the ruleset against representative docket-entry text (TODO 2026-06-17).
"""
from __future__ import annotations

from scripts.cohort_builder import classify_disposition, propose

_DECLINE = "United States' Notice of Election to Decline Intervention filed."


def _c(texts, terminated=True):
    return classify_disposition(texts, terminated)


# ── KEEP: declined + merits dismissal ───────────────────────────────────────

def test_declined_then_merits_dismissal_with_prejudice_is_keep():
    d = _c([_DECLINE,
            "ORDER granting Defendant's Motion to Dismiss the Qui Tam Complaint "
            "with prejudice for failure to plead fraud with particularity under "
            "Rule 9(b)."])
    assert d.label == "KEEP_MERITS" and d.keep and d.declination
    assert propose(d, True) == "KEEP"


def test_merits_dismissal_without_captured_declination_is_review():
    # search-only mode may miss the declination notice → REVIEW, not KEEP
    d = _c(["Order granting motion to dismiss for failure to state a claim under "
            "Rule 12(b)(6); relator failed to plead a false claim."])
    assert d.label == "KEEP_MERITS" and d.keep and not d.declination
    assert propose(d, True) == "REVIEW"


def test_summary_judgment_on_falsity_is_merits():
    d = _c([_DECLINE, "Order granting summary judgment; no genuine dispute that "
            "the claims were false or material."])
    assert d.label == "KEEP_MERITS"


# ── KEEP: declined + voluntary dismissal ────────────────────────────────────

def test_declined_then_voluntary_dismissal_is_keep_voluntary():
    d = _c([_DECLINE, "Relator's Notice of Voluntary Dismissal pursuant to "
            "Rule 41(a)(1)."])
    assert d.label == "KEEP_VOLUNTARY" and d.keep
    assert propose(d, True) == "KEEP"


# ── EXCLUDE: settlement / govt recovery (these are POSITIVES) ───────────────

def test_settlement_excluded_even_with_earlier_mtd():
    # an MTD appears earlier in the docket, but it settled → EXCLUDE wins
    d = _c(["Defendant's Motion to Dismiss the Complaint.",
            _DECLINE,
            "Stipulation of Settlement and Joint Motion to Dismiss; the parties "
            "have reached a settlement agreement."])
    assert d.label == "EXCLUDE_SETTLEMENT" and not d.keep
    assert propose(d, True) == "EXCLUDE"


def test_consent_judgment_excluded():
    d = _c(["Consent Judgment entered against defendant."])
    assert d.label == "EXCLUDE_SETTLEMENT"


def test_judgment_for_united_states_excluded():
    d = _c(["Final Judgment is entered in favor of the United States."])
    assert d.label == "EXCLUDE_SETTLEMENT"


# ── EXCLUDE: criminal / DPA ─────────────────────────────────────────────────

def test_criminal_plea_excluded():
    d = _c(["Defendant entered a guilty plea pursuant to a plea agreement."])
    assert d.label == "EXCLUDE_CRIMINAL_DPA"


def test_deferred_prosecution_excluded():
    d = _c(["The parties filed a deferred prosecution agreement."])
    assert d.label == "EXCLUDE_CRIMINAL_DPA"


# ── EXCLUDE: jurisdictional bars (not merits) ───────────────────────────────

def test_first_to_file_bar_excluded():
    d = _c([_DECLINE, "Order dismissing under the first-to-file bar, "
            "31 U.S.C. 3730(b)(5)."])
    assert d.label == "EXCLUDE_JURISDICTIONAL"


def test_public_disclosure_bar_excluded():
    d = _c(["Dismissed under the public-disclosure bar; relator is not an "
            "original source under 3730(e)."])
    assert d.label == "EXCLUDE_JURISDICTIONAL"


def test_appointments_clause_excluded():
    d = _c(["Order dismissing on Appointments Clause / Article II grounds; "
            "the qui tam provision is unconstitutional."])
    assert d.label == "EXCLUDE_CONSTITUTIONAL"


def test_statute_of_limitations_excluded():
    d = _c(["Dismissed as time-barred by the statute of limitations."])
    assert d.label == "EXCLUDE_SOL"


# ── UNKNOWN / DROP ──────────────────────────────────────────────────────────

def test_dismissed_no_reason_is_unknown_dismiss_review():
    d = _c(["Case closed. Order of dismissal entered."], terminated=True)
    # "order ... dismiss" matches merits pattern → KEEP_MERITS/REVIEW is acceptable;
    # the point is it is NOT excluded and routes to a human.
    assert propose(d, True) in ("REVIEW", "KEEP")


def test_not_terminated_is_drop():
    d = _c(["Defendant's Motion to Dismiss is pending."], terminated=False)
    assert not d.keep
    assert propose(d, False) in ("REVIEW", "DROP")  # declination absent → DROP


def test_no_signal_is_drop():
    d = _c(["Scheduling order entered. Discovery to close in 90 days."],
           terminated=False)
    assert d.label == "UNKNOWN"
    assert propose(d, False) == "DROP"


# ── precedence: EXCLUDE always beats KEEP signals in the same docket ────────

def test_exclude_precedence_over_keep_signals():
    # contains BOTH a 9(b) merits-dismiss phrase AND a settlement phrase
    d = _c(["dismissed with prejudice under Rule 9(b)",
            "pursuant to the settlement agreement"])
    assert d.label == "EXCLUDE_SETTLEMENT"
