"""
Quorum - Persona rendering layer.

Same underlying Arbiter verdict, rendered differently depending on who's
asking -- a CFO gets a short financial-impact summary with one recommended
action; a Regional Sales Manager gets full deal-level tactical detail. Every
render is gated through access_control.check_access() first: a region-scoped
manager requesting a region outside their scope gets a denial, not a
silently-filtered empty view.

Usage:
    python3 persona_render.py --datadir . --user sales_mgr_raj_apac
    python3 persona_render.py --datadir . --user cfo_maria
    python3 persona_render.py --datadir . --user sales_mgr_lena_emea --region APAC  (denied)

Reads:
    arbiter_verdicts.json, kpi_contracts.json (via access_control)
"""
import argparse
import json
from pathlib import Path

from access_control import load_users, check_access


def render_cfo(verdict):
    lines = [
        f"[{verdict['region']}] Revenue impact: ${abs(verdict['cumulative_delta_revenue_usd']):,.0f} "
        f"over weeks {min(verdict['weeks'])}-{max(verdict['weeks'])}.",
        f"Primary driver: {verdict['dominant_driver']} "
        f"({verdict['confidence_tier']} confidence).",
    ]
    if verdict["confidence_tier"] == "HIGH":
        lines.append("Action: proceed with recommended remediation (see detail view).")
    else:
        lines.append("Action: continue monitoring before committing budget to a fix.")
    return "\n".join(lines)


def render_regional_sales_manager(verdict):
    lines = [
        f"[{verdict['region']}] Revenue down ${abs(verdict['cumulative_delta_revenue_usd']):,.0f}, "
        f"weeks {min(verdict['weeks'])}-{max(verdict['weeks'])}.",
        f"Price effect: {verdict['price_effect_pct']:+.0f}% | "
        f"Volume/competitive effect: {verdict['volume_effect_pct']:+.0f}% "
        f"(dominant: {verdict['dominant_driver']}).",
        f"Confidence: {verdict['confidence_tier']}.",
    ]
    dc = verdict.get("deal_corroboration")
    if dc:
        lines.append(f"Deal detail: {dc['lost_deals']}/{dc['total_deals']} lost, "
                      f"{dc['competitor_cited']} cite the competitor by name, "
                      f"{dc['price_cited']} cite price.")
        lines.append(f"Suggested next step: prioritize win-back outreach on the "
                      f"{dc['competitor_cited']} competitor-cited losses this week.")
    return "\n".join(lines)


PERSONA_RENDERERS = {
    "cfo": render_cfo,
    "regional_sales_manager": render_regional_sales_manager,
}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--datadir", default=".")
    parser.add_argument("--user", required=True, help="Demo user key, see access_control.DEMO_USERS")
    parser.add_argument("--region", default=None,
                         help="Filter to one region; defaults to all regions the user can see")
    args = parser.parse_args()
    d = Path(args.datadir)

    users = load_users()
    if args.user not in users:
        print(f"Unknown user '{args.user}'. Available: {list(users.keys())}")
        return
    user = users[args.user]

    with open(d / "arbiter_verdicts.json") as f:
        verdicts = json.load(f)["episode_verdicts"]

    regions_to_check = [args.region] if args.region else sorted({v["region"] for v in verdicts})

    renderer = PERSONA_RENDERERS.get(user["role"])
    if renderer is None:
        print(f"No persona renderer defined for role '{user['role']}'.")
        return

    print(f"=== View for {args.user} (role: {user['role']}, "
          f"region scope: {user['assigned_region'] or 'global'}) ===\n")

    any_shown = False
    for region in regions_to_check:
        access = check_access(user, region, "regional_revenue", d)
        if not access["allowed"]:
            print(f"[{region}] ACCESS DENIED -- {access['reason']}\n")
            continue
        matching = [v for v in verdicts if v["region"] == region]
        for v in matching:
            print(renderer(v))
            print()
            any_shown = True

    if not any_shown:
        print("No verdicts available for this user's access scope.")


if __name__ == "__main__":
    main()
