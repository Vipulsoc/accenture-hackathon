"""
Quorum detection layer.

Deliberately NON-LLM: every flagging decision here is a deterministic rule
read straight from kpi_contracts.json's materiality_threshold block. This is
the layer that decides whether a KPI movement is worth waking up the four
specialist agents (Statistician/Chronicler/Pattern/Field) for -- the LLM
never sees raw numbers until AFTER this has already decided something is
material.

Usage:
    python3 detect_movements.py --datadir .

Reads:
    kpi_contracts.json, sales_weekly.csv, deals.csv, customer_tickets.csv,
    marketing_spend.csv, and (if present) superstore_snapshot_kpi.csv

Writes:
    flagged_movements.json -- the handoff artifact for the specialist agents.
"""
import argparse
import csv
import json
import re
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

START = date(2026, 6, 1)  # matches generate_data.py week 1 start

# Keywords used to classify a support ticket as price/competitor-related.
# This mirrors kpi_contracts.json's definition for complaint_volume:
# "tickets WHERE topic IN ('price','competitor')" -- done here via simple
# keyword match rather than an LLM call, keeping detection non-LLM.
PRICE_COMPETITOR_KEYWORDS = [
    "price", "pricing", "cost", "expensive", "discount",
    "competitor", "nimbusco", "cheaper", "vendor",
]


def load_csv(path):
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def week_from_date(iso_date_str):
    d = date.fromisoformat(iso_date_str)
    return (d - START).days // 7 + 1


# ---------------------------------------------------------------------
# Detector functions -- each returns a list of flagged dicts
# ---------------------------------------------------------------------

def detect_pct_change_vs_trailing_avg(rows, region_key, week_key, value_key,
                                       threshold_pct, trailing_n=4, kpi_id=""):
    """Flags a region-week if |value - trailing_avg| / trailing_avg * 100 > threshold."""
    series = defaultdict(dict)  # region -> week -> value
    for r in rows:
        series[r[region_key]][int(r[week_key])] = float(r[value_key])

    flagged = []
    for region, weeks in series.items():
        for w in sorted(weeks):
            history = [weeks[ww] for ww in range(max(1, w - trailing_n), w) if ww in weeks]
            if not history:
                continue
            trailing_avg = sum(history) / len(history)
            if trailing_avg == 0:
                continue
            pct_change = (weeks[w] - trailing_avg) / trailing_avg * 100
            if abs(pct_change) > threshold_pct:
                flagged.append({
                    "kpi_id": kpi_id, "region": region, "week": w,
                    "value": round(weeks[w], 2),
                    "trailing_avg": round(trailing_avg, 2),
                    "pct_change": round(pct_change, 2),
                    "threshold_pct": threshold_pct,
                    "reason": f"{pct_change:+.1f}% vs {trailing_n}-week trailing avg "
                              f"(threshold: ±{threshold_pct}%)",
                })
    return flagged


def detect_pct_change_vs_prior_week(rows, region_key, week_key, value_key,
                                     threshold_pct, kpi_id=""):
    """Flags a region-week if |value - prior_week_value| / prior_week_value * 100 > threshold."""
    series = defaultdict(dict)
    for r in rows:
        series[r[region_key]][int(r[week_key])] = float(r[value_key])

    flagged = []
    for region, weeks in series.items():
        for w in sorted(weeks):
            if (w - 1) not in weeks:
                continue
            prior = weeks[w - 1]
            if prior == 0:
                continue
            pct_change = (weeks[w] - prior) / prior * 100
            if abs(pct_change) > threshold_pct:
                flagged.append({
                    "kpi_id": kpi_id, "region": region, "week": w,
                    "value": round(weeks[w], 2),
                    "prior_week_value": round(prior, 2),
                    "pct_change": round(pct_change, 2),
                    "threshold_pct": threshold_pct,
                    "reason": f"{pct_change:+.1f}% vs prior week (threshold: ±{threshold_pct}%)",
                })
    return flagged


def detect_win_rate_floor(deal_rows, floor, kpi_id=""):
    """Flags any region-week where win rate falls below the floor."""
    buckets = defaultdict(lambda: {"won": 0, "total": 0})
    for r in deal_rows:
        key = (r["region"], int(r["week_closed"]))
        buckets[key]["total"] += 1
        if r["outcome"] == "won":
            buckets[key]["won"] += 1

    flagged = []
    for (region, week), v in buckets.items():
        win_rate = v["won"] / v["total"] if v["total"] else 0
        if win_rate < floor:
            flagged.append({
                "kpi_id": kpi_id, "region": region, "week": week,
                "win_rate": round(win_rate, 3),
                "deals_won": v["won"], "deals_total": v["total"],
                "floor": floor,
                "reason": f"win rate {win_rate:.0%} on {v['total']} deals "
                          f"(floor: {floor:.0%})",
            })
    return flagged


def classify_ticket(text):
    t = text.lower()
    return any(kw in t for kw in PRICE_COMPETITOR_KEYWORDS)


def detect_complaint_spike(ticket_rows, baseline_multiple, kpi_id=""):
    """Flags region-weeks where price/competitor-tagged ticket count exceeds
    a trailing baseline by the given multiple. Tickets NOT matching the
    price/competitor keyword set are excluded entirely -- this is what keeps
    a ticket spike about an unrelated issue (e.g. a resolved outage) from
    being misread as a pricing/competitive signal."""
    by_region_week = defaultdict(lambda: {"matched": 0, "total": 0, "sample_texts": []})
    for r in ticket_rows:
        week = week_from_date(r["date"])
        key = (r["region"], week)
        by_region_week[key]["total"] += 1
        if classify_ticket(r["text"]):
            by_region_week[key]["matched"] += 1
            if len(by_region_week[key]["sample_texts"]) < 2:
                by_region_week[key]["sample_texts"].append(r["text"])

    # trailing baseline = avg matched-count across all OTHER region-weeks for that region
    region_totals = defaultdict(list)
    for (region, week), v in by_region_week.items():
        region_totals[region].append((week, v["matched"]))

    flagged = []
    for region, week_counts in region_totals.items():
        for week, count in week_counts:
            others = [c for w, c in week_counts if w != week]
            baseline = (sum(others) / len(others)) if others else 0
            baseline_floor = max(baseline, 0.5)  # avoid div-by-zero / trivial flags
            if count == 0:
                continue
            if count > baseline_floor * baseline_multiple:
                v = by_region_week[(region, week)]
                flagged.append({
                    "kpi_id": kpi_id, "region": region, "week": week,
                    "matched_ticket_count": count,
                    "total_ticket_count": v["total"],
                    "baseline": round(baseline, 2),
                    "baseline_multiple": baseline_multiple,
                    "sample_texts": v["sample_texts"],
                    "reason": f"{count} price/competitor-tagged tickets vs "
                              f"baseline {baseline:.1f} (threshold: {baseline_multiple}x)",
                })
    return flagged


def detect_margin_floor(rows, floor, kpi_id=""):
    """Flags any row (region + sub-category or region + week, whatever grain
    the source used) where profit margin falls below the floor. Used for the
    real Superstore snapshot KPI, which has no time dimension to diff against."""
    flagged = []
    for r in rows:
        margin = float(r["profit_margin_pct"])
        if margin < floor:
            flagged.append({
                "kpi_id": kpi_id, "region": r["region"], "grain_key": r["grain_key"],
                "margin_pct": margin, "floor": floor,
                "sales_usd": float(r["sales_usd"]), "n_orders": int(r["n_orders"]),
                "reason": f"margin {margin:.1f}% below floor {floor}% "
                          f"(${r['sales_usd']} sales, {r['n_orders']} orders)",
            })
    return flagged


# ---------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--datadir", default=".")
    args = parser.parse_args()
    d = Path(args.datadir)

    with open(d / "kpi_contracts.json") as f:
        contracts = {k["kpi_id"]: k for k in json.load(f)["kpis"]}

    all_flags = []

    # regional_revenue: % change vs 4-week trailing avg
    if "regional_revenue" in contracts:
        sales = load_csv(d / "sales_weekly.csv")
        thr = contracts["regional_revenue"]["materiality_threshold"]["flag_if_abs_pct_change_gt"]
        all_flags += detect_pct_change_vs_trailing_avg(
            sales, "region", "week", "revenue_usd", thr, kpi_id="regional_revenue")

    # avg_selling_price: % change vs prior week
    if "avg_selling_price" in contracts:
        sales = load_csv(d / "sales_weekly.csv")
        thr = contracts["avg_selling_price"]["materiality_threshold"]["flag_if_abs_pct_change_gt"]
        all_flags += detect_pct_change_vs_prior_week(
            sales, "region", "week", "avg_price_usd", thr, kpi_id="avg_selling_price")

    # win_rate: absolute floor
    if "win_rate" in contracts and (d / "deals.csv").exists():
        deals = load_csv(d / "deals.csv")
        floor = contracts["win_rate"]["materiality_threshold"]["flag_if_win_rate_below"]
        all_flags += detect_win_rate_floor(deals, floor, kpi_id="win_rate")

    # complaint_volume: spike vs trailing baseline (price/competitor tickets only)
    if "complaint_volume" in contracts and (d / "customer_tickets.csv").exists():
        tickets = load_csv(d / "customer_tickets.csv")
        mult = contracts["complaint_volume"]["materiality_threshold"]["flag_if_count_gt_baseline_multiple"]
        all_flags += detect_complaint_spike(tickets, mult, kpi_id="complaint_volume")

    # marketing_spend: % change vs prior week
    if "marketing_spend" in contracts and (d / "marketing_spend.csv").exists():
        mkt = load_csv(d / "marketing_spend.csv")
        thr = contracts["marketing_spend"]["materiality_threshold"]["flag_if_abs_pct_change_gt"]
        all_flags += detect_pct_change_vs_prior_week(
            mkt, "region", "week", "spend_usd", thr, kpi_id="marketing_spend")

    # profit_margin_furniture: real Superstore data, absolute floor (only if
    # ingest_superstore.py has already been run and appended this KPI)
    if "profit_margin_furniture" in contracts and (d / "superstore_snapshot_kpi.csv").exists():
        snap = load_csv(d / "superstore_snapshot_kpi.csv")
        floor = contracts["profit_margin_furniture"]["materiality_threshold"]["flag_if_margin_pct_below"]
        all_flags += detect_margin_floor(snap, floor, kpi_id="profit_margin_furniture")

    out_path = d / "flagged_movements.json"
    with open(out_path, "w") as f:
        json.dump(all_flags, f, indent=2)

    print(f"Detection complete. {len(all_flags)} material movements flagged "
          f"(rule-based, zero LLM calls).")
    print(f"Written to {out_path}\n")

    by_kpi = defaultdict(list)
    for flag in all_flags:
        by_kpi[flag["kpi_id"]].append(flag)

    for kpi_id, flags in by_kpi.items():
        print(f"--- {kpi_id} ({len(flags)} flagged) ---")
        for fl in flags:
            if "week" in fl:
                loc = f"{fl['region']} / week {fl['week']}"
            else:
                loc = f"{fl['region']} / {fl.get('grain_key')}"
            print(f"  {loc}: {fl['reason']}")
        print()


if __name__ == "__main__":
    main()
