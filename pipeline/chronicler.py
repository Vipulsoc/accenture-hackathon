"""
Quorum - Chronicler agent.

"Reads the ops logs. What did we change?"

Non-LLM (structured join, no free-text reasoning needed here -- ops_events.csv
is already structured). For every flagged movement, finds ops/change-log
events for the same region within a trailing window of the flagged week, and
attaches them as candidate causes.

Usage:
    python3 chronicler.py --datadir .

Reads:
    flagged_movements.json, ops_events.csv
Writes:
    chronicler_findings.json
"""
import argparse
import csv
import json
from collections import defaultdict
from datetime import date
from pathlib import Path

START = date(2026, 6, 1)  # matches generate_data.py / detect_movements.py
WINDOW_WEEKS_BACK = 3  # how far back an event can be and still be considered relevant


def load_csv(path):
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def week_from_date(iso_date_str):
    d = date.fromisoformat(iso_date_str)
    return (d - START).days // 7 + 1


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--datadir", default=".")
    args = parser.parse_args()
    d = Path(args.datadir)

    with open(d / "flagged_movements.json") as f:
        flags = json.load(f)

    if not (d / "ops_events.csv").exists():
        print("No ops_events.csv found -- Chronicler has no source to read, skipping.")
        return

    ops_rows = load_csv(d / "ops_events.csv")
    for r in ops_rows:
        r["_week"] = week_from_date(r["date"])

    ops_by_region = defaultdict(list)
    for r in ops_rows:
        ops_by_region[r["region"]].append(r)

    findings = []
    seen = set()
    for fl in flags:
        week = fl.get("week")
        if week is None:
            # non-weekly-grain KPI (e.g. real Superstore snapshot) -- no ops
            # log exists for that source, nothing for Chronicler to join against
            continue
        key = (fl["region"], week)
        if key in seen:
            continue
        seen.add(key)

        candidates = ops_by_region.get(fl["region"], [])
        matched = [
            {
                "event_id": r["event_id"], "event_date": r["date"],
                "event_week": r["_week"], "event_type": r["event_type"],
                "description": r["description"], "flagged_to_sales": r["flagged_to_sales"],
                "weeks_before_movement": week - r["_week"],
            }
            for r in candidates
            if 0 <= (week - r["_week"]) <= WINDOW_WEEKS_BACK
        ]

        findings.append({
            "region": fl["region"], "week": week,
            "triggering_kpi": fl["kpi_id"],
            "matched_events": matched,
            "confidence": "high" if matched else "low",
            "confidence_reason": (
                f"{len(matched)} ops event(s) found within {WINDOW_WEEKS_BACK}-week "
                f"trailing window" if matched else
                f"no ops events logged for {fl['region']} within {WINDOW_WEEKS_BACK} "
                f"weeks prior to week {week} -- either untracked or genuinely no "
                f"operational cause; do not assume absence of evidence means "
                f"absence of cause."
            ),
        })

    out_path = d / "chronicler_findings.json"
    with open(out_path, "w") as f:
        json.dump(findings, f, indent=2)

    print(f"Chronicler: {len(findings)} region-weeks checked against ops log. "
          f"Written to {out_path}\n")

    for f_ in findings:
        print(f"--- {f_['region']} week {f_['week']} (triggered by {f_['triggering_kpi']}) ---")
        if f_["matched_events"]:
            for ev in f_["matched_events"]:
                print(f"  [{ev['event_id']}] {ev['event_type']} "
                      f"({ev['weeks_before_movement']}w prior): {ev['description']}")
        else:
            print(f"  No matching ops events. {f_['confidence_reason']}")
        print()


if __name__ == "__main__":
    main()
