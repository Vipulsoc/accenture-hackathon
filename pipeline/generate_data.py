"""
Quorum synthetic data generator
Scenario: BizzGrow Inc. - 3 regions (APAC, EMEA, NA), 12 weeks of data.
APAC revenue drops in weeks 9-10 due to a price rise AND a competitor
undercutting on enterprise deals - two overlapping, hard-to-separate causes.
Also includes: one sparse-history KPI (APAC-South, a region launched in week 9),
and one low-confidence / abstention case (EMEA ticket spike with no matching
revenue movement yet).
"""
import argparse
import csv
import random
from datetime import date, timedelta
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("--outdir", default=".", help="Directory to write the generated CSVs to")
args = parser.parse_args()
OUTDIR = Path(args.outdir)
OUTDIR.mkdir(parents=True, exist_ok=True)

random.seed(42)

WEEKS = 12
START = date(2026, 6, 1)  # week 1 start
REGIONS = ["APAC", "EMEA", "NA"]

def week_start(w):
    return START + timedelta(weeks=w - 1)

# ---------------------------------------------------------------------
# 1. sales_weekly.csv  (Source: Sales DB, grain: region-week, weekly refresh)
# ---------------------------------------------------------------------
sales_rows = []
base_revenue = {"APAC": 480000, "EMEA": 410000, "NA": 560000}
base_price = {"APAC": 240, "EMEA": 260, "NA": 250}
base_units = {"APAC": 2000, "EMEA": 1577, "NA": 2240}

for region in REGIONS:
    rev = base_revenue[region]
    price = base_price[region]
    for w in range(1, WEEKS + 1):
        noise = random.uniform(-0.02, 0.02)
        price_this_week = price
        if region == "APAC" and w >= 8:
            # price rise rolled out week 8, +6%
            price_this_week = price * 1.06
        units_this_week = base_units[region] * (1 + noise)
        if region == "APAC" and w >= 9:
            # unit volume drops harder than price alone explains (competitor effect)
            units_this_week *= 0.80 if w == 9 else 0.74
        revenue_this_week = round(price_this_week * units_this_week, 2)
        sales_rows.append({
            "region": region,
            "week": w,
            "week_start": week_start(w).isoformat(),
            "revenue_usd": revenue_this_week,
            "units_sold": round(units_this_week),
            "avg_price_usd": round(price_this_week, 2),
        })

# Sparse-history KPI: APAC-South, launched week 9, only 4 weeks of history
for w in range(9, WEEKS + 1):
    units = random.randint(150, 220)
    price = 235 + random.uniform(-5, 5)
    sales_rows.append({
        "region": "APAC-South",
        "week": w,
        "week_start": week_start(w).isoformat(),
        "revenue_usd": round(units * price, 2),
        "units_sold": units,
        "avg_price_usd": round(price, 2),
    })

with open(OUTDIR / "sales_weekly.csv", "w", newline="") as f:
    writer = csv.DictWriter(f, fieldnames=sales_rows[0].keys())
    writer.writeheader()
    writer.writerows(sales_rows)

# ---------------------------------------------------------------------
# 2. ops_events.csv  (Source: Ops/change log, grain: event, event-driven refresh)
# ---------------------------------------------------------------------
ops_rows = [
    {"event_id": "OPS-101", "date": week_start(8).isoformat(), "region": "APAC",
     "event_type": "price_change", "description": "Enterprise tier price increased 6% to offset input costs",
     "logged_by": "pricing_team", "flagged_to_sales": "No"},
    {"event_id": "OPS-102", "date": week_start(9).isoformat(), "region": "APAC",
     "event_type": "competitive_intel", "description": "Field reports rival 'NimbusCo' undercutting on 2 large renewals",
     "logged_by": "regional_ops", "flagged_to_sales": "Yes"},
    {"event_id": "OPS-103", "date": week_start(6).isoformat(), "region": "EMEA",
     "event_type": "support_incident", "description": "Billing portal outage, 4 hours, resolved same day",
     "logged_by": "support_eng", "flagged_to_sales": "No"},
    {"event_id": "OPS-104", "date": week_start(9).isoformat(), "region": "APAC-South",
     "event_type": "market_launch", "description": "New sub-region go-live, limited local sales headcount",
     "logged_by": "regional_ops", "flagged_to_sales": "Yes"},
    {"event_id": "OPS-105", "date": week_start(11).isoformat(), "region": "NA",
     "event_type": "promo", "description": "Seasonal promo campaign launched, 10% discount code",
     "logged_by": "marketing", "flagged_to_sales": "Yes"},
]
with open(OUTDIR / "ops_events.csv", "w", newline="") as f:
    writer = csv.DictWriter(f, fieldnames=ops_rows[0].keys())
    writer.writeheader()
    writer.writerows(ops_rows)

# ---------------------------------------------------------------------
# 3. customer_tickets.csv (Source: Support/CRM, unstructured text, near-real-time)
# ---------------------------------------------------------------------
apac_complaints = [
    "Pricing has gone up noticeably this quarter, we're comparing with alternatives now.",
    "Your competitor NimbusCo quoted us 15% lower for a similar package.",
    "Renewal cost increase wasn't communicated clearly ahead of time.",
    "We're evaluating other vendors given the recent price change.",
    "Sales rep mentioned NimbusCo is aggressively discounting in our market.",
    "Cost is becoming a concern relative to the value we're getting.",
]
emea_tickets = [
    "Billing portal was down last week, couldn't process payment on time.",
    "Minor UI bug in the invoice export, not urgent.",
    "Asked about upgrading our plan, exploring options.",
    "Support response time was a bit slow this week.",
    "Portal outage caused a delay but issue was resolved quickly.",
]
na_tickets = [
    "Loved the new promo pricing, easy signup process.",
    "Quick question about the seasonal discount code eligibility.",
    "Great onboarding experience this month.",
]

ticket_rows = []
tid = 1000
for i, text in enumerate(apac_complaints):
    ticket_rows.append({
        "ticket_id": f"TCK-{tid}", "date": (week_start(9) + timedelta(days=i)).isoformat(),
        "region": "APAC", "channel": random.choice(["email", "call_transcript", "chat"]),
        "text": text,
    })
    tid += 1
for i, text in enumerate(emea_tickets):
    ticket_rows.append({
        "ticket_id": f"TCK-{tid}", "date": (week_start(6) + timedelta(days=i)).isoformat(),
        "region": "EMEA", "channel": random.choice(["email", "call_transcript", "chat"]),
        "text": text,
    })
    tid += 1
for i, text in enumerate(na_tickets):
    ticket_rows.append({
        "ticket_id": f"TCK-{tid}", "date": (week_start(11) + timedelta(days=i)).isoformat(),
        "region": "NA", "channel": random.choice(["email", "chat"]),
        "text": text,
    })
    tid += 1

with open(OUTDIR / "customer_tickets.csv", "w", newline="") as f:
    writer = csv.DictWriter(f, fieldnames=ticket_rows[0].keys())
    writer.writeheader()
    writer.writerows(ticket_rows)

# ---------------------------------------------------------------------
# 4. deals.csv (Source: CRM, grain: deal, daily refresh) - drives win/loss check
# ---------------------------------------------------------------------
deal_rows = []
did = 5000
for i in range(12):
    region = "APAC"
    lost_to_competitor = i < 5  # 5 of 12 explicitly cite competitor
    lost_to_price = i < 8       # 8 of 12 cite price as a factor (overlaps with above)
    outcome = "lost" if i < 10 else "won"
    reason = []
    if lost_to_price and outcome == "lost":
        reason.append("price sensitivity")
    if lost_to_competitor and outcome == "lost":
        reason.append("competitor (NimbusCo) offer")
    deal_rows.append({
        "deal_id": f"DL-{did}", "region": region, "week_closed": random.choice([9, 10]),
        "outcome": outcome, "deal_value_usd": random.randint(15000, 60000),
        "reason_notes": "; ".join(reason) if reason else "n/a",
    })
    did += 1

with open(OUTDIR / "deals.csv", "w", newline="") as f:
    writer = csv.DictWriter(f, fieldnames=deal_rows[0].keys())
    writer.writeheader()
    writer.writerows(deal_rows)

# ---------------------------------------------------------------------
# 5. marketing_spend.csv (Source: Marketing platform, grain: region-week)
# ---------------------------------------------------------------------
mkt_rows = []
base_spend = {"APAC": 25000, "EMEA": 22000, "NA": 30000}
for region in REGIONS:
    for w in range(1, WEEKS + 1):
        spend = base_spend[region] * (1 + random.uniform(-0.05, 0.05))
        if region == "NA" and w >= 11:
            spend *= 1.4  # promo campaign
        mkt_rows.append({"region": region, "week": w, "spend_usd": round(spend, 2)})

with open(OUTDIR / "marketing_spend.csv", "w", newline="") as f:
    writer = csv.DictWriter(f, fieldnames=mkt_rows[0].keys())
    writer.writeheader()
    writer.writerows(mkt_rows)

print("Generated: sales_weekly.csv, ops_events.csv, customer_tickets.csv, deals.csv, marketing_spend.csv")
print(f"sales rows: {len(sales_rows)}, ops rows: {len(ops_rows)}, ticket rows: {len(ticket_rows)}, "
      f"deal rows: {len(deal_rows)}, marketing rows: {len(mkt_rows)}")
