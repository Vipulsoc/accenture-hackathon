"""
Ingest the public 'Sample Superstore' dataset (Kaggle / mirrors) and convert it
into a second, REAL-data KPI that plugs into the same Quorum pipeline as the
synthetic APAC/EMEA/NA story.

Usage:
    1. Download the dataset (Kaggle: "Superstore Dataset", or the GitHub mirror
       https://github.com/muskaanpirani/Analysis_of_retail_store -> SampleSuperstore.csv)
    2. Place it at /mnt/user-data/uploads/SampleSuperstore.csv (or pass --input)
    3. Run: python3 ingest_superstore.py

Output:
    superstore_weekly_kpi.csv   -- region x week Profit Margin %, matching the
                                    'region-week' grain used by kpi_contracts.json
    Appends a new KPI entry (profit_margin_furniture) to kpi_contracts.json

Why this KPI: Superstore's real Furniture category has a genuine, well-known
margin problem (heavy discounting erodes profit) -- so unlike the synthetic
APAC story, this is a naturally occurring anomaly, not scripted. It's meant to
demonstrate the system generalizes beyond one hand-built scenario.
"""
import argparse
import csv
import json
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path


def load_superstore(path: Path):
    suffix = path.suffix.lower()
    if suffix in (".xlsx", ".xls"):
        return _load_excel(path)
    return _load_csv(path)


def _load_csv(path: Path):
    rows = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        # Normalize column names across common variants (Kaggle vs mirrors)
        fieldmap = {k.strip().lower().replace(" ", "_").replace("-", "_"): k for k in reader.fieldnames}
        for raw in reader:
            row = {k: raw[v] for k, v in fieldmap.items()}
            rows.append(row)
    return rows, fieldmap


def _load_excel(path: Path):
    try:
        import openpyxl
    except ImportError:
        print("ERROR: reading .xlsx requires openpyxl. Install it with:")
        print("    pip install openpyxl")
        print("...or open the file in Excel and 'Save As' CSV, then re-run with that .csv file.")
        sys.exit(1)

    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    ws = wb.active  # first sheet -- Kaggle Superstore usually has data on sheet 1
    rows_iter = ws.iter_rows(values_only=True)
    header = [str(h).strip() for h in next(rows_iter)]
    fieldmap = {h.lower().replace(" ", "_").replace("-", "_"): h for h in header}
    rows = []
    for raw_vals in rows_iter:
        raw = dict(zip(header, raw_vals))
        row = {}
        for k, orig_h in fieldmap.items():
            val = raw.get(orig_h)
            row[k] = "" if val is None else str(val)
        rows.append(row)
    return rows, fieldmap


def parse_date(s):
    for fmt in ("%m/%d/%Y", "%d/%m/%Y", "%Y-%m-%d", "%m-%d-%Y",
                "%Y-%m-%d %H:%M:%S", "%m/%d/%Y %H:%M:%S"):
        try:
            return datetime.strptime(s.strip(), fmt)
        except ValueError:
            continue
    raise ValueError(f"Unrecognized date format: {s}")


def iso_week_key(dt: datetime):
    iso = dt.isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


def _aggregate_weekly(rows, category):
    agg = defaultdict(lambda: {"sales": 0.0, "profit": 0.0, "n_orders": 0})
    skipped = 0
    for r in rows:
        if r["category"].strip().lower() != category.lower():
            continue
        try:
            dt = parse_date(r["order_date"])
        except ValueError:
            skipped += 1
            continue
        key = (r["region"].strip(), iso_week_key(dt))
        try:
            sales = float(r["sales"])
            profit = float(r["profit"])
        except ValueError:
            skipped += 1
            continue
        agg[key]["sales"] += sales
        agg[key]["profit"] += profit
        agg[key]["n_orders"] += 1

    if skipped:
        print(f"Skipped {skipped} rows with unparseable date/numeric fields.")

    out_rows = []
    for (region, week), vals in sorted(agg.items()):
        margin_pct = (vals["profit"] / vals["sales"] * 100) if vals["sales"] else 0.0
        out_rows.append({
            "region": region,
            "grain_key": week,
            "category": category,
            "sales_usd": round(vals["sales"], 2),
            "profit_usd": round(vals["profit"], 2),
            "profit_margin_pct": round(margin_pct, 2),
            "n_orders": vals["n_orders"],
        })
    return out_rows


def _aggregate_snapshot(rows, category):
    """Fallback when there's no date column: aggregate by region + sub-category
    instead of region + week. Still real data, just a snapshot rather than a
    trend -- use sub-category as the second dimension so there's still enough
    granularity for a Statistician-style contribution breakdown."""
    agg = defaultdict(lambda: {"sales": 0.0, "profit": 0.0, "n_orders": 0})
    skipped = 0
    for r in rows:
        if r["category"].strip().lower() != category.lower():
            continue
        sub = r.get("sub_category", "n/a").strip() or "n/a"
        key = (r["region"].strip(), sub)
        try:
            sales = float(r["sales"])
            profit = float(r["profit"])
        except ValueError:
            skipped += 1
            continue
        agg[key]["sales"] += sales
        agg[key]["profit"] += profit
        agg[key]["n_orders"] += 1

    if skipped:
        print(f"Skipped {skipped} rows with unparseable numeric fields.")

    out_rows = []
    for (region, sub), vals in sorted(agg.items()):
        margin_pct = (vals["profit"] / vals["sales"] * 100) if vals["sales"] else 0.0
        out_rows.append({
            "region": region,
            "grain_key": sub,  # sub-category, standing in for the missing week dimension
            "category": category,
            "sales_usd": round(vals["sales"], 2),
            "profit_usd": round(vals["profit"], 2),
            "profit_margin_pct": round(margin_pct, 2),
            "n_orders": vals["n_orders"],
        })
    return out_rows


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default="/mnt/user-data/uploads/SampleSuperstore.csv")
    parser.add_argument("--outdir", default=".")
    parser.add_argument("--category", default="Furniture",
                         help="Product category to isolate as the KPI (default: Furniture, "
                              "the well-known low-margin category in this dataset)")
    args = parser.parse_args()

    in_path = Path(args.input)
    category_arg = args.category
    if not in_path.exists():
        print(f"ERROR: input file not found at {in_path}")
        print("Download the Superstore dataset and place it at that path, "
              "or pass --input /path/to/file.csv")
        sys.exit(1)

    rows, fieldmap = load_superstore(in_path)
    print(f"Loaded {len(rows)} rows. Columns detected: {list(fieldmap.keys())}")

    required_base = {"region", "category", "sales", "profit"}
    missing_base = required_base - set(fieldmap.keys())
    if missing_base:
        print(f"ERROR: missing expected columns after normalization: {missing_base}")
        print("This dataset variant may use different column names -- inspect "
              "fieldmap above and adjust load_superstore() if needed.")
        sys.exit(1)

    has_dates = "order_date" in fieldmap
    if not has_dates:
        print("NOTE: no 'Order Date' column found in this file. This is the "
              "reduced Superstore variant (Ship Mode/Segment/Region/Category/"
              "Sub-Category/Sales/Quantity/Discount/Profit only, no order-level "
              "dates). Falling back to a REGION-LEVEL SNAPSHOT aggregation "
              "instead of a weekly time series -- still real, unscripted data, "
              "just not a trend over time. If you want the weekly version, "
              "download the full Superstore dataset (search Kaggle for "
              "'Global Superstore' or the 9994-row version WITH Order Date/"
              "Order ID/Customer Name columns).")

    if has_dates:
        out_rows = _aggregate_weekly(rows, args.category)
    else:
        out_rows = _aggregate_snapshot(rows, args.category)

    if not out_rows:
        print(f"ERROR: no rows matched category='{args.category}'. "
              f"Check the category name against your file (case-insensitive match used).")
        sys.exit(1)

    out_path = Path(args.outdir) / "superstore_snapshot_kpi.csv"
    grain_label = "region-week" if has_dates else "region-subcategory (snapshot, no dates in source)"
    with open(out_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=out_rows[0].keys())
        writer.writeheader()
        writer.writerows(out_rows)

    print(f"Wrote {len(out_rows)} rows ({grain_label} grain) to {out_path}")

    flagged = [r for r in out_rows if r["profit_margin_pct"] < 0]
    print(f"Rows with NEGATIVE profit margin (real data, unscripted): {len(flagged)}")
    for r in flagged[:8]:
        print(f"  {r['region']} / {r['grain_key']}: margin {r['profit_margin_pct']}% "
              f"on ${r['sales_usd']} sales ({r['n_orders']} orders)")

    # Append a KPI contract entry for this real-data KPI
    contract_path = Path(args.outdir) / "kpi_contracts.json"
    new_kpi = {
        "kpi_id": "profit_margin_furniture",
        "name": "Furniture Category Profit Margin (real Superstore data)",
        "definition": f"Profit / Sales, in %, per region per {'ISO week' if has_dates else 'sub-category'}, "
                       f"for the {category_arg} category.",
        "formula": f"SUM(profit_usd) / SUM(sales_usd) * 100 GROUP BY region, "
                   f"{'iso_week' if has_dates else 'sub_category'} WHERE category='{category_arg}'",
        "grain": grain_label,
        "source_system": "superstore_snapshot_kpi.csv (public Superstore dataset)",
        "refresh_cadence": "weekly" if has_dates else "static (one-time snapshot -- no timestamps in source)",
        "materiality_threshold": {
            "method": "absolute margin below floor (level metric, not delta)",
            "flag_if_margin_pct_below": 0.0
        },
        "owner": "Category Manager - Furniture",
        "lineage": ["Public Superstore data -> aggregation -> superstore_snapshot_kpi.csv"],
        "access_tags": {
            "row_level": "region",
            "column_level": "none",
            "roles_allowed": ["cfo", "regional_sales_manager", "category_manager"]
        },
        "note": "Real, unscripted dataset -- included to demonstrate the pipeline "
                "generalizes beyond the hand-built APAC/EMEA/NA scenario." +
                ("" if has_dates else " NOTE: this source file had no order-level "
                 "dates, so this KPI is a static snapshot, not a weekly trend -- "
                 "swap in the full dated dataset if a live trend is needed for the demo.")
    }

    if contract_path.exists():
        with open(contract_path) as f:
            contracts = json.load(f)
        existing_ids = {k["kpi_id"] for k in contracts.get("kpis", [])}
        if new_kpi["kpi_id"] not in existing_ids:
            contracts["kpis"].append(new_kpi)
            with open(contract_path, "w") as f:
                json.dump(contracts, f, indent=2)
            print(f"Appended '{new_kpi['kpi_id']}' to {contract_path}")
        else:
            print(f"'{new_kpi['kpi_id']}' already present in {contract_path}, skipped.")
    else:
        print(f"No existing kpi_contracts.json found at {contract_path} -- "
              f"copy your existing one into --outdir first, or run without "
              f"expecting auto-merge.")


if __name__ == "__main__":
    main()
