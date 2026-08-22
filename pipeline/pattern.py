"""
Quorum - Pattern agent.

"Matches past cases. Seen this shape before?"

Non-LLM. For every flagged region-week, looks across ALL regions' full
revenue history (not just the flagged region) for weeks with a similarly
shaped % change vs trailing average, using the same trailing-baseline method
detect_movements.py used to flag things in the first place. Deliberately
excludes the flagged week itself and its own immediate run (the current
"episode") so it isn't just matching itself.

Explicitly returns "no reliable analog" rather than forcing a weak match --
this matters most for APAC-South, which only has 1-4 weeks of history and
should never be reported as having a confident historical pattern.

Usage:
    python3 pattern.py --datadir .

Reads:
    flagged_movements.json, sales_weekly.csv
Writes:
    pattern_findings.json
"""
import argparse
import csv
import json
from collections import defaultdict
from pathlib import Path

TRAILING_N = 4
SIMILARITY_PCT_TOLERANCE = 6.0  # analog must be within this many pct-points of the flagged pct_change
MIN_HISTORY_WEEKS_FOR_CONFIDENT_MATCH = 4


def load_csv(path):
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def build_series(sales_rows):
    series = defaultdict(dict)
    for r in sales_rows:
        series[r["region"]][int(r["week"])] = float(r["revenue_usd"])
    return series


def pct_change_series(series):
    """For every region-week with enough trailing history, compute pct change
    vs trailing avg. Returns dict[(region, week)] -> pct_change."""
    out = {}
    for region, weeks in series.items():
        for w in sorted(weeks):
            history = [weeks[ww] for ww in range(max(1, w - TRAILING_N), w) if ww in weeks]
            if not history:
                continue
            trailing_avg = sum(history) / len(history)
            if trailing_avg == 0:
                continue
            out[(region, w)] = {
                "pct_change": (weeks[w] - trailing_avg) / trailing_avg * 100,
                "n_history": len(history),
            }
    return out


def find_analogs(all_pct_changes, target_region, target_week, target_pct,
                  own_region_history_weeks):
    """Search every region-week (excluding the target region's own current
    'episode' -- i.e. weeks that are themselves already part of a flagged
    run for that region -- to avoid a movement matching itself trivially)."""
    candidates = []
    for (region, week), data in all_pct_changes.items():
        if region == target_region and week in own_region_history_weeks:
            continue  # don't match against the current episode's own weeks
        diff = abs(data["pct_change"] - target_pct)
        if diff <= SIMILARITY_PCT_TOLERANCE:
            candidates.append({
                "region": region, "week": week,
                "pct_change": round(data["pct_change"], 1),
                "diff_from_target": round(diff, 1),
                "history_weeks_available": data["n_history"],
            })
    candidates.sort(key=lambda c: c["diff_from_target"])
    return candidates


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--datadir", default=".")
    args = parser.parse_args()
    d = Path(args.datadir)

    with open(d / "flagged_movements.json") as f:
        flags = json.load(f)

    sales_rows = load_csv(d / "sales_weekly.csv")
    series = build_series(sales_rows)
    all_pct_changes = pct_change_series(series)

    # Treat all flagged weeks for a region as one "episode" so Pattern
    # doesn't match APAC week 10 against APAC week 9, etc.
    episode_weeks_by_region = defaultdict(set)
    for fl in flags:
        if fl["kpi_id"] == "regional_revenue":
            episode_weeks_by_region[fl["region"]].add(fl["week"])

    findings = []
    seen = set()
    for fl in flags:
        if fl["kpi_id"] != "regional_revenue":
            continue
        key = (fl["region"], fl["week"])
        if key in seen:
            continue
        seen.add(key)

        region, week = fl["region"], fl["week"]
        target_data = all_pct_changes.get((region, week))
        history_available = target_data["n_history"] if target_data else 0

        if history_available < 2:
            findings.append({
                "region": region, "week": week,
                "confidence": "no_analog",
                "confidence_reason": f"only {history_available} trailing week(s) of "
                                      f"history for {region} itself -- too little data "
                                      f"to responsibly claim a historical pattern match. "
                                      f"Reporting 'no reliable analog' rather than forcing one.",
                "analogs": [],
            })
            continue

        target_pct = target_data["pct_change"]
        analogs = find_analogs(all_pct_changes, region, week, target_pct,
                                episode_weeks_by_region[region])

        if not analogs:
            findings.append({
                "region": region, "week": week,
                "target_pct_change": round(target_pct, 1),
                "confidence": "no_analog",
                "confidence_reason": f"no other region-week in the dataset shows a "
                                      f"similarly shaped movement (within "
                                      f"±{SIMILARITY_PCT_TOLERANCE}pp) -- this appears "
                                      f"to be a novel pattern, not a recurring one.",
                "analogs": [],
            })
        else:
            best = analogs[0]
            target_solid = history_available >= MIN_HISTORY_WEEKS_FOR_CONFIDENT_MATCH
            analog_solid = best["history_weeks_available"] >= MIN_HISTORY_WEEKS_FOR_CONFIDENT_MATCH
            if target_solid and analog_solid:
                confidence = "high"
                reason_suffix = "both target and analog have solid trailing history"
            elif target_solid or analog_solid:
                confidence = "medium"
                reason_suffix = ("target has solid history but the best analog is "
                                  "itself thin on history" if target_solid else
                                  "analog has solid history but the target itself is thin on history")
            else:
                confidence = "low"
                reason_suffix = "both target and analog have limited trailing history"

            findings.append({
                "region": region, "week": week,
                "target_pct_change": round(target_pct, 1),
                "confidence": confidence,
                "confidence_reason": f"{len(analogs)} historical analog(s) found within "
                                      f"±{SIMILARITY_PCT_TOLERANCE}pp -- {reason_suffix}",
                "analogs": analogs[:3],  # top 3 closest matches
            })

    out_path = d / "pattern_findings.json"
    with open(out_path, "w") as f:
        json.dump(findings, f, indent=2)

    print(f"Pattern: {len(findings)} region-weeks checked for historical analogs. "
          f"Written to {out_path}\n")

    for f_ in findings:
        print(f"--- {f_['region']} week {f_['week']} (confidence: {f_['confidence']}) ---")
        if f_["analogs"]:
            for a in f_["analogs"]:
                print(f"  Analog: {a['region']} week {a['week']} "
                      f"({a['pct_change']:+.1f}%, diff {a['diff_from_target']}pp, "
                      f"{a['history_weeks_available']} weeks history)")
        else:
            print(f"  {f_['confidence_reason']}")
        print()


if __name__ == "__main__":
    main()
