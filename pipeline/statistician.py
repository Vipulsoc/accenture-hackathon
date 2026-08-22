"""
Quorum - Statistician agent.

"Reads the numbers. Where did the gap sit?"

Non-LLM. For every flagged regional_revenue movement, decomposes the change
into PRICE effect, VOLUME effect, and INTERACTION (mix) effect, vs. the same
4-week trailing baseline the detection layer used -- so the Statistician's
baseline always matches the one that triggered the flag in the first place.

Classic bridge decomposition:
    delta_revenue = (P1*Q1) - (P0*Q0)
    price_effect       = (P1 - P0) * Q0
    volume_effect       = (Q1 - Q0) * P0
    interaction_effect  = (P1 - P0) * (Q1 - Q0)
    price_effect + volume_effect + interaction_effect == delta_revenue  (exact)

For flagged movements on other KPIs (win_rate, marketing_spend, etc.) the
Statistician reports the numeric shift as-is -- decomposition only applies
where we have both a price and a volume component (i.e. revenue).

Usage:
    python3 statistician.py --datadir .

Reads:
    flagged_movements.json, sales_weekly.csv, kpi_contracts.json
Writes:
    statistician_findings.json
"""
import argparse
import csv
import json
from collections import defaultdict
from pathlib import Path

TRAILING_N = 4


def load_csv(path):
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def build_series(sales_rows):
    """region -> week -> {revenue, price, units}"""
    series = defaultdict(dict)
    for r in sales_rows:
        series[r["region"]][int(r["week"])] = {
            "revenue": float(r["revenue_usd"]),
            "price": float(r["avg_price_usd"]),
            "units": float(r["units_sold"]),
        }
    return series


def trailing_baseline(series_for_region, week, field, n=TRAILING_N):
    history = [series_for_region[w][field] for w in range(max(1, week - n), week)
               if w in series_for_region]
    if not history:
        return None, 0
    return sum(history) / len(history), len(history)


def decompose_revenue(series, region, week):
    if week not in series.get(region, {}):
        return None

    current = series[region][week]
    p1, q1 = current["price"], current["units"]

    p0, p0_n = trailing_baseline(series[region], week, "price")
    q0, q0_n = trailing_baseline(series[region], week, "units")

    if p0 is None or q0 is None:
        return {
            "region": region, "week": week,
            "confidence": "low",
            "confidence_reason": f"insufficient trailing history "
                                  f"({min(p0_n, q0_n)} of {TRAILING_N} weeks available) "
                                  f"-- decomposition unreliable, treat as directional only.",
            "current_price": p1, "current_units": q1,
        }

    delta_revenue = (p1 * q1) - (p0 * q0)
    price_effect = (p1 - p0) * q0
    volume_effect = (q1 - q0) * p0
    interaction_effect = (p1 - p0) * (q1 - q0)

    total_check = price_effect + volume_effect + interaction_effect
    price_share = price_effect / delta_revenue * 100 if delta_revenue else 0
    volume_share = volume_effect / delta_revenue * 100 if delta_revenue else 0
    interaction_share = interaction_effect / delta_revenue * 100 if delta_revenue else 0

    n_weeks = min(p0_n, q0_n)
    confidence = "high" if n_weeks >= TRAILING_N else ("medium" if n_weeks >= 2 else "low")

    return {
        "region": region, "week": week,
        "baseline_price": round(p0, 2), "baseline_units": round(q0, 2),
        "current_price": round(p1, 2), "current_units": round(q1, 2),
        "delta_revenue_usd": round(delta_revenue, 2),
        "price_effect_usd": round(price_effect, 2),
        "volume_effect_usd": round(volume_effect, 2),
        "interaction_effect_usd": round(interaction_effect, 2),
        "price_effect_pct_of_total": round(price_share, 1),
        "volume_effect_pct_of_total": round(volume_share, 1),
        "interaction_effect_pct_of_total": round(interaction_share, 1),
        "reconciliation_check_usd": round(total_check - delta_revenue, 6),  # should be ~0
        "confidence": confidence,
        "confidence_reason": f"{n_weeks}/{TRAILING_N} trailing weeks available for baseline",
        "trailing_weeks_used": n_weeks,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--datadir", default=".")
    args = parser.parse_args()
    d = Path(args.datadir)

    with open(d / "flagged_movements.json") as f:
        flags = json.load(f)

    sales_rows = load_csv(d / "sales_weekly.csv")
    series = build_series(sales_rows)

    findings = {"revenue_decompositions": [], "other_kpi_summaries": []}

    seen_revenue_keys = set()
    for fl in flags:
        if fl["kpi_id"] == "regional_revenue":
            key = (fl["region"], fl["week"])
            if key in seen_revenue_keys:
                continue
            seen_revenue_keys.add(key)
            result = decompose_revenue(series, fl["region"], fl["week"])
            if result:
                findings["revenue_decompositions"].append(result)
        else:
            findings["other_kpi_summaries"].append({
                "kpi_id": fl["kpi_id"],
                "region": fl["region"],
                "week": fl.get("week", fl.get("grain_key")),
                "reason": fl["reason"],
            })

    out_path = d / "statistician_findings.json"
    with open(out_path, "w") as f:
        json.dump(findings, f, indent=2)

    print(f"Statistician: {len(findings['revenue_decompositions'])} revenue "
          f"decompositions, {len(findings['other_kpi_summaries'])} other KPI "
          f"summaries. Written to {out_path}\n")

    for r in findings["revenue_decompositions"]:
        print(f"--- {r['region']} week {r['week']} (confidence: {r['confidence']}) ---")
        if "delta_revenue_usd" in r:
            print(f"  Revenue change: ${r['delta_revenue_usd']:,.0f}")
            print(f"    Price effect:       ${r['price_effect_usd']:>10,.0f}  "
                  f"({r['price_effect_pct_of_total']:>6.1f}% of change)")
            print(f"    Volume effect:      ${r['volume_effect_usd']:>10,.0f}  "
                  f"({r['volume_effect_pct_of_total']:>6.1f}% of change)")
            print(f"    Interaction/mix:    ${r['interaction_effect_usd']:>10,.0f}  "
                  f"({r['interaction_effect_pct_of_total']:>6.1f}% of change)")
        else:
            print(f"  {r['confidence_reason']}")
        print()


if __name__ == "__main__":
    main()
