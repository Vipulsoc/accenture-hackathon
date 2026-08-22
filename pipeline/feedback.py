"""
Quorum - Feedback loop.

Closes the gap Arbiter's docstring explicitly flags as unbuilt: agent trust
weights start as static priors, but should be LEARNED from real analyst
override/confirmation history. This script is that learning mechanism.

How it works:
1. An analyst reviews an episode's verdict and records their final call
   (e.g. "actually it's the competitor, confirmed" or "insufficient
   evidence, don't act yet").
2. For that episode, we already have each specialist's original signal
   sitting in falsifier_findings.json (Statistician's dominant_driver,
   Chronicler's ops_corroborated, Pattern's confidence, Field's
   field_corroborated). We score each agent as "agreed" or "disagreed"
   with the analyst's final call using simple, explainable rules (see
   _score_agent_agreement below).
3. Every feedback event is appended to feedback_log.json (audit trail,
   never overwritten).
4. Agent weights are recomputed as a Bayesian-style blend of the static
   prior and the observed agreement rate, using a shrinkage estimator so
   a handful of early feedback events can't wildly swing the weights:

       blended = (prior * PRIOR_STRENGTH + observed_rate * n_samples)
                 / (PRIOR_STRENGTH + n_samples)

   This is written to agent_weights.json. arbiter.py loads this file if
   it exists (with enough samples) instead of its own hardcoded priors --
   that's the actual "closes the loop" step.

Usage:
    python3 feedback.py --datadir . --region APAC --weeks 9,10,11,12 \\
        --verdict volume --analyst raj --notes "Confirmed via win-loss review, NimbusCo cited on 5 deals"

    --verdict choices:
        price               -- analyst confirms price was the dominant cause
        volume              -- analyst confirms volume/competitive pressure was dominant
        insufficient_evidence -- analyst says there wasn't enough to call it either way
        false_positive      -- (for complaint_flag episodes) analyst confirms the
                                flag was NOT a real signal
        confirmed_signal    -- (for complaint_flag episodes) analyst confirms the
                                flag WAS a real signal

    To just recompute weights from existing feedback without adding a new
    entry:
        python3 feedback.py --datadir . --recompute-only
"""
import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

# Same static priors as arbiter.py's AGENT_WEIGHTS -- used as the starting
# point before any feedback exists, and as the "pull" that observed data
# gets blended against.
STATIC_PRIORS = {
    "statistician": 0.90,
    "chronicler": 0.85,
    "field": 0.70,
    "pattern": 0.50,
}

# How many "equivalent prior observations" the static prior is worth.
# Higher = weights move more slowly as real feedback comes in (more
# conservative); lower = weights adapt faster. 5 means it takes roughly
# 5 pieces of real feedback to meaningfully shift a weight away from prior.
PRIOR_STRENGTH = 5


def load_json(path, default=None):
    if not path.exists():
        return default
    with open(path) as f:
        return json.load(f)


def _score_agent_agreement(analyst_verdict, week_entries, complaint_entry):
    """Returns {agent: True/False/None} -- None means 'not applicable/no
    signal to score for this episode'. Scoring rules are intentionally
    simple and explainable, not a black box:

    - statistician: did its numeric dominant_driver match the analyst's
      price/volume call?
    - chronicler: did it find a corroborating ops event, and does that
      match whether the analyst found real evidence (vs insufficient)?
    - pattern: was its confidence tier well-CALIBRATED -- i.e. did it
      correctly signal low confidence when evidence turned out to be
      insufficient, or reasonable confidence when it wasn't? Pattern
      doesn't make a directional claim, so it's scored on calibration,
      not direction.
    - field: for revenue episodes, did its corroboration boolean align
      with whether the analyst found real evidence? For complaint_flag
      episodes, did Falsifier's CONFIRMED/REJECTED verdict (which is
      driven entirely by Field's confidence) match the analyst's call?
    """
    scores = {"statistician": None, "chronicler": None, "pattern": None, "field": None}

    if week_entries:
        stat_agree, chron_agree, pattern_agree, field_agree = [], [], [], []
        for w in week_entries:
            if analyst_verdict in ("price", "volume"):
                stat_agree.append(w["dominant_driver"] == analyst_verdict)
                # chronicler: agree if it found a corroborating event (real evidence exists)
                chron_agree.append(bool(w.get("ops_corroborated")))
                pattern_agree.append(w["pattern_confidence"] in ("high", "medium"))
                if w.get("field_corroborated") is not None:
                    field_agree.append(w["field_corroborated"] is True)
            elif analyst_verdict == "insufficient_evidence":
                stat_agree.append(w["overall_confidence"] == "low")
                chron_agree.append(not w.get("ops_corroborated"))
                pattern_agree.append(w["pattern_confidence"] in ("low", "no_analog"))
                if w.get("field_corroborated") is not None:
                    field_agree.append(w["field_corroborated"] is False)

        if stat_agree:
            scores["statistician"] = sum(stat_agree) / len(stat_agree) >= 0.5
        if chron_agree:
            scores["chronicler"] = sum(chron_agree) / len(chron_agree) >= 0.5
        if pattern_agree:
            scores["pattern"] = sum(pattern_agree) / len(pattern_agree) >= 0.5
        if field_agree:
            scores["field"] = sum(field_agree) / len(field_agree) >= 0.5

    if complaint_entry:
        if analyst_verdict == "false_positive":
            scores["field"] = complaint_entry["verdict"] == "REJECTED"
        elif analyst_verdict == "confirmed_signal":
            scores["field"] = complaint_entry["verdict"] == "CONFIRMED"

    return scores


def record_feedback(datadir, region, weeks, analyst_verdict, analyst, notes):
    d = Path(datadir)
    falsifier = load_json(d / "falsifier_findings.json", [])

    week_entries = [f for f in falsifier if f["test"] == "revenue_movement"
                     and f["region"] == region and f["week"] in weeks]
    complaint_entries = [f for f in falsifier if f["test"] == "complaint_flag"
                          and f["region"] == region and f["week"] in weeks]
    complaint_entry = complaint_entries[0] if complaint_entries else None

    if not week_entries and not complaint_entry:
        print(f"WARNING: no falsifier findings found for {region} weeks {weeks} -- "
              f"nothing to score. Check region/week values against falsifier_findings.json.")

    scores = _score_agent_agreement(analyst_verdict, week_entries, complaint_entry)

    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "region": region, "weeks": weeks,
        "analyst_verdict": analyst_verdict, "analyst": analyst, "notes": notes,
        "agent_agreement": scores,
    }

    log_path = d / "feedback_log.json"
    log = load_json(log_path, [])
    log.append(entry)
    with open(log_path, "w") as f:
        json.dump(log, f, indent=2)

    print(f"Feedback recorded: {region} weeks {weeks} -> analyst verdict '{analyst_verdict}'")
    for agent, agree in scores.items():
        status = "N/A" if agree is None else ("AGREED" if agree else "DISAGREED")
        print(f"  {agent:<14} {status}")

    return log


def recompute_weights(datadir, log=None):
    d = Path(datadir)
    if log is None:
        log = load_json(d / "feedback_log.json", [])

    weights = {}
    sample_counts = {}
    for agent, prior in STATIC_PRIORS.items():
        judgments = [e["agent_agreement"][agent] for e in log
                     if e["agent_agreement"].get(agent) is not None]
        n = len(judgments)
        observed_rate = sum(1 for j in judgments if j) / n if n else prior
        blended = (prior * PRIOR_STRENGTH + observed_rate * n) / (PRIOR_STRENGTH + n)
        weights[agent] = round(blended, 4)
        sample_counts[agent] = n

    out = {
        "weights": weights,
        "sample_counts": sample_counts,
        "total_feedback_events": len(log),
        "prior_strength": PRIOR_STRENGTH,
        "last_updated": datetime.now(timezone.utc).isoformat(),
        "note": "Blend of static priors and observed analyst-agreement rate "
                "(Bayesian-style shrinkage -- see PRIOR_STRENGTH in feedback.py). "
                "arbiter.py loads this file automatically if present.",
    }
    out_path = d / "agent_weights.json"
    with open(out_path, "w") as f:
        json.dump(out, f, indent=2)

    print(f"\nWeights recomputed from {len(log)} feedback event(s), written to {out_path}:")
    for agent, w in weights.items():
        print(f"  {agent:<14} {w:.3f}  (prior {STATIC_PRIORS[agent]:.2f}, "
              f"{sample_counts[agent]} sample(s))")
    return out


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--datadir", default=".")
    parser.add_argument("--region")
    parser.add_argument("--weeks", help="Comma-separated week numbers, e.g. 9,10,11,12")
    parser.add_argument("--verdict", choices=["price", "volume", "insufficient_evidence",
                                               "false_positive", "confirmed_signal"])
    parser.add_argument("--analyst", default="unknown")
    parser.add_argument("--notes", default="")
    parser.add_argument("--recompute-only", action="store_true",
                         help="Skip recording new feedback, just recompute "
                              "agent_weights.json from existing feedback_log.json")
    args = parser.parse_args()

    if args.recompute_only:
        recompute_weights(args.datadir)
        return

    if not (args.region and args.weeks and args.verdict):
        parser.error("--region, --weeks, and --verdict are required unless --recompute-only")

    weeks = [int(w.strip()) for w in args.weeks.split(",")]
    log = record_feedback(args.datadir, args.region, weeks, args.verdict, args.analyst, args.notes)
    recompute_weights(args.datadir, log)


if __name__ == "__main__":
    main()
