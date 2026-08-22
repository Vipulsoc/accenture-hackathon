"""
Quorum - Falsifier stage.

"Tries to disprove every claim. Weak ideas die here."

Non-LLM. Reconciles the four specialists' outputs against each other via
deterministic rules -- no free-text judgment needed here since all four
inputs are already structured JSON. Two things this stage specifically does:

1. For each region-week with a Statistician revenue decomposition, tests
   whether the numerically dominant driver (price vs. volume/mix) has
   corroborating evidence in Chronicler's matched ops events, Field's ticket
   summary (where available), and Pattern's historical confidence -- and
   assigns an overall confidence tier based on how much independent
   corroboration survives.

2. For complaint_volume-triggered flags, checks Field's own self-reported
   confidence: a flag whose only "evidence" is a keyword match, but whose
   actual ticket content Field found to be positive/low-confidence, gets
   explicitly marked REJECTED rather than silently treated as validated
   just because it was flagged.

Usage:
    python3 falsifier.py --datadir .

Reads:
    flagged_movements.json, statistician_findings.json,
    chronicler_findings.json, pattern_findings.json, field_findings.json
Writes:
    falsifier_findings.json
"""
import argparse
import json
from pathlib import Path


def load_json(path, default=None):
    if not path.exists():
        return default
    with open(path) as f:
        return json.load(f)


def index_by_region_week(items, key_func):
    idx = {}
    for it in items:
        idx.setdefault(key_func(it), []).append(it)
    return idx


def event_types_for(chronicler_entry):
    return {ev["event_type"] for ev in chronicler_entry.get("matched_events", [])} if chronicler_entry else set()


def falsify_revenue_movement(decomp, chronicler_entry, pattern_entry, field_entries):
    region, week = decomp["region"], decomp["week"]
    notes = []

    if "price_effect_pct_of_total" not in decomp:
        return {
            "region": region, "week": week, "test": "revenue_movement",
            "verdict": "INSUFFICIENT_DATA",
            "notes": [decomp.get("confidence_reason", "no decomposition available")],
            "overall_confidence": "low",
        }

    price_pct = decomp["price_effect_pct_of_total"]
    volume_pct = decomp["volume_effect_pct_of_total"]
    dominant = "price" if abs(price_pct) > abs(volume_pct) else "volume"
    notes.append(f"Numeric decomposition: price={price_pct:+.1f}% of change, "
                 f"volume={volume_pct:+.1f}% of change -> dominant driver = {dominant}")

    # Does the dominant driver have a matching ops event?
    types = event_types_for(chronicler_entry)
    expected_type = "price_change" if dominant == "price" else None
    corroborating_types = {"competitive_intel", "market_launch"} if dominant == "volume" else {"price_change"}
    ops_corroborated = bool(types & corroborating_types)

    if ops_corroborated:
        matched = [ev for ev in chronicler_entry["matched_events"] if ev["event_type"] in corroborating_types]
        notes.append(f"Ops log CORROBORATES {dominant} hypothesis: " +
                     "; ".join(f"[{m['event_id']}] {m['description']}" for m in matched))
    elif types:
        notes.append(f"Ops log found events ({', '.join(types)}) but none directly "
                      f"support the {dominant}-dominant hypothesis -- other factors may be at play.")
    else:
        notes.append("No ops events found at all for this region-week -- "
                      "numeric movement has no logged operational cause on file.")

    # Field corroboration (only present for complaint_volume-triggered region-weeks)
    field_corroborated = None
    if field_entries:
        fe = field_entries[0]
        field_corroborated = fe["confidence"] == "HIGH"
        notes.append(f"Field (customer tickets) confidence: {fe['confidence']} "
                     f"({fe['n_tickets_summarized']} tickets summarized)")

    # Pattern confidence
    pattern_conf = pattern_entry["confidence"] if pattern_entry else "unknown"
    notes.append(f"Pattern (historical analog) confidence: {pattern_conf}")

    # Score corroboration
    score = 0
    if ops_corroborated:
        score += 2
    elif types:
        score += 0  # neutral, not contradicting
    else:
        score -= 1
    if field_corroborated is True:
        score += 2
    elif field_corroborated is False:
        score -= 1
    if pattern_conf in ("high", "medium"):
        score += 1
    elif pattern_conf == "no_analog":
        score -= 1

    if score >= 3:
        confidence = "high"
    elif score >= 1:
        confidence = "medium"
    else:
        confidence = "low"

    return {
        "region": region, "week": week, "test": "revenue_movement",
        "dominant_driver": dominant,
        "price_effect_pct": price_pct, "volume_effect_pct": volume_pct,
        "ops_corroborated": ops_corroborated,
        "field_corroborated": field_corroborated,
        "pattern_confidence": pattern_conf,
        "verdict": "SURVIVES" if confidence != "low" else "SURVIVES_WEAKLY",
        "overall_confidence": confidence,
        "notes": notes,
    }


def falsify_complaint_flag(flag, field_entry):
    region, week = flag["region"], flag["week"]
    if not field_entry:
        return {
            "region": region, "week": week, "test": "complaint_flag",
            "verdict": "UNTESTED", "overall_confidence": "unknown",
            "notes": ["No Field investigation available for this flag."],
        }

    if field_entry["confidence"] == "HIGH" and field_entry["n_tickets_summarized"] > 0:
        return {
            "region": region, "week": week, "test": "complaint_flag",
            "verdict": "CONFIRMED", "overall_confidence": "high",
            "notes": [f"Field confirmed the complaint_volume flag with HIGH confidence "
                      f"across {field_entry['n_tickets_summarized']} tickets: "
                      f"{field_entry['summary'][:150]}..."],
        }
    else:
        return {
            "region": region, "week": week, "test": "complaint_flag",
            "verdict": "REJECTED", "overall_confidence": "low",
            "notes": [f"complaint_volume flag triggered on keyword match alone, but Field's "
                      f"actual reading of the ticket text came back {field_entry['confidence']} "
                      f"confidence -- this appears to be a FALSE POSITIVE of the keyword-based "
                      f"detector (e.g. positive/neutral mentions of 'pricing' or 'discount' "
                      f"misclassified as complaints). Do not treat as a validated signal.",
                      f"Field summary: {field_entry['summary'][:200]}..."],
        }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--datadir", default=".")
    args = parser.parse_args()
    d = Path(args.datadir)

    flags = load_json(d / "flagged_movements.json", [])
    statistician = load_json(d / "statistician_findings.json", {"revenue_decompositions": []})
    chronicler = load_json(d / "chronicler_findings.json", [])
    pattern = load_json(d / "pattern_findings.json", [])
    field = load_json(d / "field_findings.json", [])

    chronicler_idx = {(c["region"], c["week"]): c for c in chronicler}
    pattern_idx = {(p["region"], p["week"]): p for p in pattern}
    field_idx = index_by_region_week(field, lambda f: (f["region"], f["week"]))

    results = []

    # Test 1: every revenue decomposition
    for decomp in statistician.get("revenue_decompositions", []):
        key = (decomp["region"], decomp["week"])
        result = falsify_revenue_movement(
            decomp, chronicler_idx.get(key), pattern_idx.get(key), field_idx.get(key))
        results.append(result)

    # Test 2: every complaint_volume flag
    for fl in flags:
        if fl["kpi_id"] != "complaint_volume":
            continue
        key = (fl["region"], fl["week"])
        result = falsify_complaint_flag(fl, field_idx.get(key, [None])[0])
        results.append(result)

    out_path = d / "falsifier_findings.json"
    with open(out_path, "w") as f:
        json.dump(results, f, indent=2)

    print(f"Falsifier: {len(results)} claims tested. Written to {out_path}\n")
    for r in results:
        print(f"--- {r['region']} week {r['week']} [{r['test']}] "
              f"-> {r['verdict']} (confidence: {r['overall_confidence']}) ---")
        for n in r["notes"]:
            print(f"  {n}")
        print()


if __name__ == "__main__":
    main()
