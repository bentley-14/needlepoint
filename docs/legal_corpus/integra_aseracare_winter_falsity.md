---
doctrine: statistical_outlier_pleading_and_objective_falsity
citation: "United States ex rel. Integra Med Analytics, L.L.C. v. Baylor Scott & White Health, 816 F. App'x 892 (5th Cir. 2020); United States v. AseraCare, Inc., 938 F.3d 1278 (11th Cir. 2019); Winter ex rel. United States v. Gardens Regional Hospital & Medical Center, Inc., 953 F.3d 1108 (9th Cir. 2020)"
court_year: "5th Cir. 2020; 11th Cir. 2019; 9th Cir. 2020"
verification_status: verified
elements:
  - "Integra (5th Cir.): statistical/data-mining evidence that is 'equally consistent with a legal and obvious alternative explanation' does not satisfy Rule 8/9(b) — the outlier must be tied to a legal theory of falsity the data cannot otherwise explain, not just an extreme number."
  - "AseraCare (11th Cir.): in a medical-necessity FCA case, a mere difference of reasonable clinical judgment/opinion between experts, without more, is NOT sufficient to establish falsity — falsity requires an objectively false statement (a certification not supported by any reasonable reading of the medical record, not simply one that a later reviewer disagrees with)."
  - "Winter v. Gardens Regional (9th Cir.): a physician's clinical judgment CAN be false for FCA purposes — AseraCare's holding does not create a blanket rule that medical opinions are immune from falsity; the 9th Circuit rejected a per se rule that clinical judgment cannot be objectively false and held it can be, if the judgment was not honestly held or lacked a reasonable basis."
our_application_rule: >
  Do not plead a statistical-outlier count without pairing it with a falsity anchor —
  Integra is the 5th Circuit's clearest statement that extremity alone, however dramatic,
  is dismissible if an innocent explanation is equally consistent with the same data
  (this is the doctrinal backbone of counsel_build.md's convergence-floor / magnitude-only
  gate). For medical-necessity theories specifically, understand the AseraCare/Winter
  split: AseraCare (11th Cir.) protects a good-faith clinical judgment that a later
  reviewer merely disagrees with — a battle of expert opinion is not falsity. Winter v.
  Gardens Regional (9th Cir.) is the necessary counter-authority: clinical judgment IS
  falsifiable when the judgment was not honestly/reasonably held (e.g., a documented
  template-driven certification untethered to the individual patient's actual condition,
  or a certification pattern inconsistent with the provider's own contemporaneous
  clinical notes). When pleading a medical-necessity count, identify which posture
  applies: if the only evidence is "our expert disagrees with their coding," treat the
  count as AseraCare-vulnerable and look for the Winter-style anchor — evidence the
  certification was not an honestly-held clinical judgment at all (a fixed template,
  volume/pattern impossible to reconcile with individualized assessment, or a documented
  disconnect between the chart and the billed complexity).
common_rebuttals:
  - "Defense: the statistical pattern is equally consistent with an innocent explanation — a large practice, a sicker patient panel, a legitimate specialty mix (the core Integra defense) — this is DISPOSITIVE unless the count has a non-magnitude falsity anchor (per-se violation, LCD criteria failure, confirmed-fact anchor) independent of the statistic."
  - "Defense: this is a difference of reasonable clinical judgment, not falsity (AseraCare) — rebut ONLY if there is a Winter-style anchor showing the judgment was not honestly/reasonably held; absent that anchor, this attack should be treated as likely dispositive and the count reframed or not pled on medical-necessity grounds alone."
  - "Defense: the relator's screen would flag many other providers nationally, proving it's not selective for fraud (data_dredging, Integra-adjacent) — rebut/ground with `scripts/cohort_screen_count.py`; a small national match count defeats this attack, a large one confirms it."
---

# Integra Outlier-Pleading + AseraCare/Winter Objective-Falsity Split

## Holding

**Integra (5th Cir.)**: *United States ex rel. Integra Med Analytics, L.L.C. v. Baylor
Scott & White Health*, 816 F. App'x 892 (5th Cir. 2020), affirmed dismissal of a
data-mining FCA suit alleging Baylor systematically upcoded complication/comorbidity
codes based on statistical analysis of billing data. The Fifth Circuit held that
statistical evidence which is "equally consistent with a legal and obvious alternative
explanation" as with fraud does not satisfy pleading requirements — an unpublished but
frequently-cited opinion that has become the leading circuit-level statement against
pure data-mining FCA theories.

**AseraCare (11th Cir.)**: *United States v. AseraCare, Inc.*, 938 F.3d 1278 (11th Cir.
2019), a hospice medical-necessity case, held that a mere difference of reasonable
clinical judgment between medical experts, without more, cannot support an FCA falsity
finding — the government's expert disagreeing with the certifying physician's judgment
does not, by itself, make the certification objectively false.

**Winter v. Gardens Regional (9th Cir.)**: *Winter ex rel. United States v. Gardens
Regional Hospital & Medical Center, Inc.*, 953 F.3d 1108 (9th Cir. 2020), expressly
declined to adopt a categorical rule (as some readings of AseraCare suggest) that a
physician's medical judgment can never be false for FCA purposes. The Ninth Circuit held
clinical judgment CAN be objectively false — if the certifying physician did not
honestly hold the judgment, or the judgment lacked any reasonable basis given the
patient's actual condition. This preserves FCA liability for medical-necessity fraud
where the "clinical judgment" defense is a pretext rather than a genuine, individualized
assessment.

## Our application

Two distinct but related doctrines converge in this card:

1. **Statistical outlier pleading (Integra)** is the doctrinal root of the
   `counsel_build.md` convergence-floor rule: "no matter how large the excess," a count
   resting solely on peer-excess magnitude is not independent convergence and is
   dismissible as equally consistent with an innocent high-volume-but-legitimate
   practice. Four Integra-type attacks appear in the taxonomy (2 survived by the relator,
   2 mitigated) — outlier-only extremity predictably draws it, and it is knowable at
   build time.

2. **Medical-necessity falsity (AseraCare/Winter split)** governs any count alleging a
   provider certified medical necessity falsely. Before pleading such a count, classify
   it:
   - **AseraCare-vulnerable**: the only evidence is a documentation/coding disagreement
     between reviewers — treat as weak, likely dismissible on falsity alone absent more.
   - **Winter-anchored**: there is evidence the certification was not an honestly-held,
     individualized judgment — a fixed template applied regardless of patient
     presentation, a volume of identical certifications physically/temporally
     inconsistent with individualized review, or a documented conflict between the
     chart's actual findings and the billed complexity/necessity level. This anchor is
     what makes a medical-necessity count pleadable notwithstanding AseraCare.

## Common rebuttals / defense playbook

See frontmatter. The Integra defense is dispositive absent a non-magnitude falsity
anchor; the AseraCare defense is dispositive absent a Winter-style honest-judgment
defect; ground any data-dredging adjacent attack with `cohort_screen_count.py`.

## Verification note

All three citations independently verified via CourtListener and/or WebSearch. Integra:
CourtListener title search did not directly surface the unpublished 816 F. App'x 892
opinion by name, but the citation and holding were independently confirmed via WebSearch
(Arnall Golden Gregory LLP client alert; U.S. Chamber of Commerce case tracker) — "the
Fifth Circuit rejected the relator's reliance on statistical data... 'consistent with a
legal and obvious alternative explanation.'" This cite is also present, verification_status
`landmark`, in the existing production doctrine corpus (`data/ground_truth/doctrine_corpus.jsonl`,
entry `integra-5th-baylor`), consistent with this independent check. AseraCare: verified
via CourtListener `verify` — *United States v. Aseracare, Inc.*, 938 F.3d 1278, 11th Cir.,
decided 2019-09-09, exact-cite match. Winter v. Gardens Regional: verified via
CourtListener `verify` — 953 F.3d 1108, 9th Cir., decided 2020-03-23, exact-cite match.
