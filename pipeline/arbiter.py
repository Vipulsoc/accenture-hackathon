"""
Quorum - Arbiter stage.

"Weighs each specialist by their past accuracy."

Groups individual region-week results into full "episodes" (e.g. APAC weeks
9-12 is one continuous story, not four separate ones), computes a
revenue-weighted attribution split across the episode, and produces the
final natural-language verdict -- matching the "Price rise 58% * Rival 31% *
not yet separable, check X" style from the original pitch deck, now backed
by the actual numbers from every stage of the pipeline.

HONEST STATUS: "weigh by past accuracy" now has a real mechanism behind it
via feedback.py, which scores each agent's agreement with analyst-confirmed
outcomes and writes learned weights to agent_weights.json (Bayesian-style
blend of static priors and observed accuracy, so a handful of early
feedback events can't wildly swing things). Until feedback.py has been run
at least once, Arbiter falls back to a fixed set of prior trust weights
(numeric/structured sources trusted more than free-text LLM judgment,
which is trusted more than thin historical pattern-matching) -- so on a
fresh install this is still a prior, not yet a learned weight, until real
feedback accumulates. See feedback.py for the full mechanism.

Usage:
    python3 arbiter.py --datadir .

Reads:
    falsifier_findings.json, statistician_findings.json,
    chronicler_findings.json, field_findings.json, deals.csv
Writes:
    arbiter_verdicts.json (also prints the human-readable verdict)
"""
import argparse
import csv
import json
from collections import defaultdict
from pathlib import Path

# Static prior trust weights, used until real feedback exists. Once
# feedback.py has recorded analyst overrides/confirmations, it writes
# agent_weights.json with weights blended from these priors and observed
# accuracy -- load_agent_weights() below picks that up automatically.
AGENT_WEIGHTS_STATIC = {
    "statistician": 0.90,   # deterministic arithmetic on real numbers
    "chronicler": 0.85,     # structured ops-log join, high precision
    "field": 0.70,          # LLM judgment on free text, generally reliable but not infallible
    "pattern": 0.50,        # thin historical base in this dataset, weakest signal
}


def load_agent_weights(datadir):
    """Loads learned weights from agent_weights.json (written by
    feedback.py) if present and backed by at least one feedback event;
    otherwise falls back to the static priors. This is the actual
    "closes the loop" mechanism -- Arbiter's weighting improves as
    feedback.py records more analyst outcomes. Returns (weights, is_learned)."""
    path = Path(datadir) / "agent_weights.json"
    if path.exists():
        with open(path) as f:
            data = json.load(f)
        if data.get("total_feedback_events", 0) > 0:
            print(f"Arbiter: using LEARNED weights from {data['total_feedback_events']} "
                  f"feedback event(s) (agent_weights.json).")
            return data["weights"], True
    print("Arbiter: no feedback history yet -- using static prior weights. "
          "Run feedback.py after analyst review to start learning real weights.")
    return AGENT_WEIGHTS_STATIC, False


def load_json(path, default=None):
    if not path.exists():
        return default
    with open(path) as f:
        return json.load(f)


def load_csv(path):
    if not path.exists():
        return []
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def confidence_to_score(conf):
    return {"high": 1.0, "medium": 0.6, "low": 0.3, "no_analog": 0.1, "unknown": 0.3}.get(conf, 0.3)


def synthesize_episode(region, weeks_data, deals_rows, agent_weights, weights_are_learned):
    """weeks_data: list of falsifier revenue_movement results for this region,
    each already merged with its statistician decomposition."""
    total_abs_delta = sum(abs(w["delta_revenue_usd"]) for w in weeks_data)
    if total_abs_delta == 0:
        weighted_price_pct = weighted_volume_pct = 0
    else:
        weighted_price_pct = sum(w["price_effect_pct"] * abs(w["delta_revenue_usd"])
                                  for w in weeks_data) / total_abs_delta
        weighted_volume_pct = sum(w["volume_effect_pct"] * abs(w["delta_revenue_usd"])
                                   for w in weeks_data) / total_abs_delta

    cumulative_delta = sum(w["delta_revenue_usd"] for w in weeks_data)

    # Weighted confidence score across the episode using AGENT_WEIGHTS priors
    scores = []
    for w in weeks_data:
        scores.append(agent_weights["statistician"] * confidence_to_score(w["overall_confidence"]))
        if w.get("ops_corroborated"):
            scores.append(agent_weights["chronicler"] * 1.0)
        if w.get("field_corroborated") is True:
            scores.append(agent_weights["field"] * 1.0)
        elif w.get("field_corroborated") is False:
            scores.append(agent_weights["field"] * 0.2)
        scores.append(agent_weights["pattern"] * confidence_to_score(w["pattern_confidence"]))
    episode_score = sum(scores) / len(scores) if scores else 0

    if episode_score >= 0.7:
        tier = "HIGH"
    elif episode_score >= 0.45:
        tier = "MEDIUM"
    else:
        tier = "LOW"

    dominant = "volume/competitive pressure" if abs(weighted_volume_pct) > abs(weighted_price_pct) else "price"

    # Deal-level corroboration for the recommended next check
    region_deals = [dl for dl in deals_rows if dl["region"] == region]
    lost = [dl for dl in region_deals if dl["outcome"] == "lost"]
    competitor_cited = [dl for dl in lost if "competitor" in dl["reason_notes"].lower()
                         or "nimbusco" in dl["reason_notes"].lower()]
    price_cited = [dl for dl in lost if "price" in dl["reason_notes"].lower()]

    verdict_lines = [
        f"{region}: revenue down ${abs(cumulative_delta):,.0f} cumulative across "
        f"weeks {min(w['week'] for w in weeks_data)}-{max(w['week'] for w in weeks_data)}.",
        f"Attribution -- Price effect: {weighted_price_pct:+.0f}% of movement "
        f"| Volume/competitive effect: {weighted_volume_pct:+.0f}% of movement "
        f"(dominant driver: {dominant}).",
        f"Confidence: {tier} (episode score {episode_score:.2f}, based on "
        f"{'LEARNED weights from analyst feedback' if weights_are_learned else 'static prior weights, no feedback yet'}).",
    ]
    if region_deals:
        both_ids = {dl["deal_id"] for dl in price_cited} & {dl["deal_id"] for dl in competitor_cited}
        verdict_lines.append(
            f"Deal-level check: {len(lost)}/{len(region_deals)} lost deals in {region}, "
            f"{len(competitor_cited)} cite the competitor, {len(price_cited)} cite price "
            f"({len(both_ids)} cite both).")
    if tier != "HIGH":
        verdict_lines.append(
            "RECOMMENDATION: confidence not yet high enough for a firm attribution -- "
            "continue monitoring next 2-4 weeks before final action.")
    else:
        verdict_lines.append(
            f"RECOMMENDATION: review win/loss reasons on the {len(competitor_cited)} "
            f"competitor-cited lost deals to quantify displaced revenue precisely.")

    return {
        "region": region,
        "weeks": sorted(w["week"] for w in weeks_data),
        "cumulative_delta_revenue_usd": round(cumulative_delta, 2),
        "price_effect_pct": round(weighted_price_pct, 1),
        "volume_effect_pct": round(weighted_volume_pct, 1),
        "dominant_driver": dominant,
        "episode_confidence_score": round(episode_score, 3),
        "confidence_tier": tier,
        "deal_corroboration": {
            "lost_deals": len(lost), "total_deals": len(region_deals),
            "competitor_cited": len(competitor_cited), "price_cited": len(price_cited),
        } if region_deals else None,
        "verdict_text": " ".join(verdict_lines),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--datadir", default=".")
    args = parser.parse_args()
    d = Path(args.datadir)

    falsifier = load_json(d / "falsifier_findings.json", [])
    deals_rows = load_csv(d / "deals.csv")
    agent_weights, weights_are_learned = load_agent_weights(args.datadir)

    revenue_results = [r for r in falsifier if r["test"] == "revenue_movement"]

    # Need delta_revenue_usd on each -- pull from statistician_findings.json
    statistician = load_json(d / "statistician_findings.json", {"revenue_decompositions": []})
    stat_idx = {(s["region"], s["week"]): s for s in statistician.get("revenue_decompositions", [])}
    for r in revenue_results:
        stat = stat_idx.get((r["region"], r["week"]), {})
        r["delta_revenue_usd"] = stat.get("delta_revenue_usd", 0)

    by_region = defaultdict(list)
    for r in revenue_results:
        by_region[r["region"]].append(r)

    verdicts = []
    for region, weeks_data in by_region.items():
        verdicts.append(synthesize_episode(region, weeks_data, deals_rows, agent_weights, weights_are_learned))

    # Complaint-flag verdicts pass through directly (already resolved by Falsifier)
    complaint_results = [r for r in falsifier if r["test"] == "complaint_flag"]
    complaint_verdicts = []
    for r in complaint_results:
        if r["verdict"] == "REJECTED":
            text = (f"{r['region']} week {r['week']}: complaint_volume alert was investigated "
                    f"and REJECTED as a false positive -- {r['notes'][0]}")
        elif r["verdict"] == "CONFIRMED":
            text = (f"{r['region']} week {r['week']}: complaint_volume alert CONFIRMED "
                    f"by Field investigation (folded into the revenue episode verdict above).")
        else:
            text = f"{r['region']} week {r['week']}: complaint_volume alert untested."
        complaint_verdicts.append({"region": r["region"], "week": r["week"],
                                    "verdict": r["verdict"], "text": text})

    out_path = d / "arbiter_verdicts.json"
    with open(out_path, "w") as f:
        json.dump({"episode_verdicts": verdicts, "complaint_flag_verdicts": complaint_verdicts}, f, indent=2)

    print(f"Arbiter: {len(verdicts)} episode verdict(s), {len(complaint_verdicts)} "
          f"complaint-flag verdict(s). Written to {out_path}\n")

    print("=" * 70)
    print("FINAL VERDICTS")
    print("=" * 70)
    for v in verdicts:
        print(f"\n[{v['confidence_tier']}] {v['verdict_text']}")
    print()
    for c in complaint_verdicts:
        print(f"\n{c['text']}")


if __name__ == "__main__":
    main()
