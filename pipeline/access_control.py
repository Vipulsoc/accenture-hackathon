"""
Quorum - Access control / entitlement middleware.

Enforces the row_level access tags already defined in kpi_contracts.json
(e.g. regional_revenue.access_tags.row_level = "region") -- this is the
piece that turns those contract tags from documentation into an actually
enforced rule, and produces the audit trail the brief asks for.

Model: each user has a role and, if their role is region-scoped, an
assigned_region. A request for data outside that scope is DENIED and
logged, not silently filtered -- the audit trail should show every denial
attempt, not just successful queries.

Usage (as a library, called by persona_render.py):
    from access_control import check_access, load_users

    users = load_users()
    result = check_access(users["priya_apac_manager"], region="EMEA",
                           kpi_id="regional_revenue", datadir=".")
    if not result["allowed"]:
        ...

Usage (standalone demo):
    python3 access_control.py --datadir .

Reads:
    kpi_contracts.json
Writes:
    access_audit_log.json (appends across runs)
"""
import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

# Demo user directory. In a real system this would come from an identity
# provider (SSO/SCIM) -- hardcoded here since building auth is out of scope
# for a hackathon prototype, but the enforcement logic below is real.
DEMO_USERS = {
    "cfo_maria": {"role": "cfo", "assigned_region": None},  # None = global access
    "sales_mgr_raj_apac": {"role": "regional_sales_manager", "assigned_region": "APAC"},
    "sales_mgr_lena_emea": {"role": "regional_sales_manager", "assigned_region": "EMEA"},
    "category_mgr_furniture": {"role": "category_manager", "assigned_region": None},
}


def load_users():
    return DEMO_USERS


def load_kpi_contracts(datadir):
    with open(Path(datadir) / "kpi_contracts.json") as f:
        return {k["kpi_id"]: k for k in json.load(f)["kpis"]}


def check_access(user, region, kpi_id, datadir, log=True):
    """Returns {"allowed": bool, "reason": str} and appends an audit log
    entry (unless log=False, used for read-only checks)."""
    contracts = load_kpi_contracts(datadir)
    kpi = contracts.get(kpi_id)

    if kpi is None:
        result = {"allowed": False, "reason": f"Unknown KPI '{kpi_id}' -- no contract on file."}
    elif user["role"] not in kpi["access_tags"]["roles_allowed"]:
        result = {"allowed": False,
                   "reason": f"Role '{user['role']}' is not in roles_allowed "
                             f"{kpi['access_tags']['roles_allowed']} for KPI '{kpi_id}'."}
    elif kpi["access_tags"]["row_level"] == "region" and user["assigned_region"] is not None \
            and user["assigned_region"] != region:
        result = {"allowed": False,
                   "reason": f"Row-level region scope violation: user is scoped to "
                             f"'{user['assigned_region']}' but requested data for '{region}'."}
    else:
        result = {"allowed": True, "reason": "Access granted within role and region scope."}

    if log:
        _append_audit_log(datadir, user, region, kpi_id, result)
    return result


def _append_audit_log(datadir, user, region, kpi_id, result):
    log_path = Path(datadir) / "access_audit_log.json"
    entries = []
    if log_path.exists():
        with open(log_path) as f:
            entries = json.load(f)
    entries.append({
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "user_role": user["role"], "user_assigned_region": user["assigned_region"],
        "requested_region": region, "requested_kpi": kpi_id,
        "allowed": result["allowed"], "reason": result["reason"],
    })
    with open(log_path, "w") as f:
        json.dump(entries, f, indent=2)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--datadir", default=".")
    args = parser.parse_args()
    d = args.datadir

    # Demo scenario matching the Round 2 checklist requirement: an
    # EMEA-scoped regional_sales_manager attempting to query APAC data.
    users = load_users()
    scenarios = [
        ("cfo_maria", "APAC", "regional_revenue"),
        ("sales_mgr_raj_apac", "APAC", "regional_revenue"),
        ("sales_mgr_lena_emea", "APAC", "regional_revenue"),  # DENIED: wrong region
        ("sales_mgr_lena_emea", "EMEA", "regional_revenue"),
        ("category_mgr_furniture", "APAC", "win_rate"),  # DENIED: role not in win_rate's roles_allowed
    ]

    print(f"{'User':<25} {'Requested':<20} {'KPI':<25} {'Result'}")
    print("-" * 90)
    for user_key, region, kpi_id in scenarios:
        user = users[user_key]
        result = check_access(user, region, kpi_id, d)
        status = "ALLOWED" if result["allowed"] else "DENIED"
        print(f"{user_key:<25} {region:<20} {kpi_id:<25} {status}")
        if not result["allowed"]:
            print(f"    -> {result['reason']}")

    print(f"\nAudit log written/appended to {Path(d) / 'access_audit_log.json'}")


if __name__ == "__main__":
    main()
