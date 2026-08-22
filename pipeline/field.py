"""
Quorum - Field agent.

"Asks the people closest. What did you actually see?"

This is the ONE specialist that calls an LLM -- all the others (Statistician,
Chronicler, Pattern) are pure rules/arithmetic. Field's job is retrieval +
summarization of free text, which genuinely needs language understanding
rather than a keyword count.

Retrieval is still non-LLM and deterministic (reuses the same price/
competitor keyword classifier as detect_movements.py, so the tickets Field
summarizes are exactly the ones that triggered the complaint_volume flag --
no scope drift between detection and explanation). Only the SUMMARIZATION
step calls the LLM.

Regions/weeks with zero matching tickets (e.g. EMEA) are explicitly reported
as "no relevant customer signal" -- Field must never fabricate a summary from
an empty ticket set.

LLM calls go through OpenRouter (https://openrouter.ai), an OpenAI-compatible
gateway to many providers -- this lets you use a genuinely free-tier model
(e.g. Meta Llama 3.3 70B) instead of paying per-call. Swap --model to any
OpenRouter model ID, including a paid Claude model later if you want to
compare quality for the final demo.

Usage:
    pip install openai
    export OPENROUTER_API_KEY=sk-or-v1-...
    python3 field.py --datadir .
    python3 field.py --datadir . --model "meta-llama/llama-3.3-70b-instruct:free"
"""
import argparse
import csv
import json
import os
import sys
import time
from collections import defaultdict
from datetime import date
from pathlib import Path

START = date(2026, 6, 1)  # matches generate_data.py / detect_movements.py

PRICE_COMPETITOR_KEYWORDS = [
    "price", "pricing", "cost", "expensive", "discount",
    "competitor", "nimbusco", "cheaper", "vendor",
]

DEFAULT_MODEL = "nvidia/nemotron-3-ultra-550b-a55b:free"

# Free-tier model availability on OpenRouter shifts over time, AND some
# OpenRouter accounts restrict calls to an explicit "Allowed Models"
# guardrail (Settings -> Allowed Models) to prevent accidental billing --
# any model NOT on that list gets rejected regardless of whether it's free
# elsewhere. PRIMARY_CANDIDATES below are confirmed present on this
# project's actual allowlist, ORDERED BY OBSERVED RELIABILITY for this
# summarization task: Ultra has cleanly produced HIGH-confidence, correctly
# suppressed (non-leaking) summaries across multiple live runs. Lightning
# repeatedly leaked raw chain-of-thought despite the "detailed thinking off"
# directive, and GLM-5.2 has been unavailable/unsuitable when tried -- both
# kept in the chain as fallback since a model that struggles on one occasion
# may still work when Ultra is itself rate-limited or overloaded.
# SECONDARY_CANDIDATES are other commonly-free models kept as a further
# fallback in case the allowlist is ever widened. If your own OpenRouter
# account has a different Allowed Models list, check Settings -> Allowed
# Models and update PRIMARY_CANDIDATES to match.
PRIMARY_CANDIDATES = [
    "nvidia/nemotron-3-ultra-550b-a55b:free",
    "nvidia/nemotron-3.5-lightning:free",
    "z-ai/glm-5.2:free",
]
SECONDARY_CANDIDATES = [
    "meta-llama/llama-3.3-70b-instruct:free",
    "google/gemini-2.0-flash-exp:free",
    "z-ai/glm-4.5-air:free",
    "qwen/qwen3-235b-a22b:free",
    "deepseek/deepseek-chat-v3.1:free",
    "mistralai/mistral-small-3.2-24b-instruct:free",
]
FALLBACK_FREE_MODELS = PRIMARY_CANDIDATES + SECONDARY_CANDIDATES

# Approximate $/1M tokens -- ILLUSTRATIVE ONLY. Free-tier models are
# genuinely $0. Paid rates change over time and vary by provider/route on
# OpenRouter; verify current pricing at openrouter.ai/models before citing
# exact figures in a report. Purpose here is to demonstrate the cost-
# telemetry MECHANISM the brief asks for, not to be a live pricing feed.
APPROX_PRICING_PER_1M_TOKENS = {
    # free-tier models used in this project
    "nvidia/nemotron-3.5-lightning:free": {"input": 0, "output": 0},
    "nvidia/nemotron-3-ultra-550b-a55b:free": {"input": 0, "output": 0},
    "z-ai/glm-5.2:free": {"input": 0, "output": 0},
    "meta-llama/llama-3.3-70b-instruct:free": {"input": 0, "output": 0},
    "google/gemini-2.0-flash-exp:free": {"input": 0, "output": 0},
    "z-ai/glm-4.5-air:free": {"input": 0, "output": 0},
    "qwen/qwen3-235b-a22b:free": {"input": 0, "output": 0},
    "deepseek/deepseek-chat-v3.1:free": {"input": 0, "output": 0},
    "mistralai/mistral-small-3.2-24b-instruct:free": {"input": 0, "output": 0},
    # illustrative paid comparison point -- CHECK CURRENT RATE before using for real
    "anthropic/claude-sonnet-4.5": {"input": 3.00, "output": 15.00},
}

_telemetry_log = []  # populated by _try_model, written out at end of main()

SYSTEM_PROMPT = """You are the "Field" specialist in a multi-agent business-intelligence \
system. Your ONLY job is to summarize what customers are saying in a set of support \
tickets/call notes for one region and time window. You are NOT the Statistician (no \
numbers/revenue claims) and NOT the Chronicler (no claims about internal company events \
unless a ticket explicitly mentions them).

Rules:
- Summarize ONLY what is in the provided tickets. Do not infer causes not stated in the text.
- If tickets express mixed or contradictory sentiment (e.g. some positive, some negative), say so explicitly -- do not average them into a falsely neutral summary.
- Note any recurring named entities (competitor names, specific complaints) verbatim if they appear.
- Keep the summary to 2-4 sentences.
- End with a one-line confidence note: HIGH if the tickets are unambiguous and consistent, LOW if they are sparse, mixed, or ambiguous.
- If given zero tickets, say so plainly and do not speculate.
"""


def load_csv(path):
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def week_from_date(iso_date_str):
    d = date.fromisoformat(iso_date_str)
    return (d - START).days // 7 + 1


def classify_ticket(text):
    t = text.lower()
    return any(kw in t for kw in PRICE_COMPETITOR_KEYWORDS)


_working_model_cache = {}  # avoid re-probing a dead model on every call


def _try_model(client, model, system_prompt, user_prompt, region=None, week=None):
    """Single attempt against one model. Raises on failure.

    Some free-tier models (NVIDIA Nemotron family in particular) are
    reasoning models that emit a large internal chain-of-thought BEFORE the
    actual answer -- large enough that even a generous max_tokens can get
    exhausted mid-thought, and OpenRouter's generic reasoning.exclude flag
    isn't honored by every provider route. NVIDIA's own docs specify the
    correct off-switch for Nemotron specifically: prepending the literal
    system-prompt directive "detailed thinking off" (with temperature=0)
    turns off extended reasoning at the source, rather than trying to
    suppress/strip it after the fact. Combined with reasoning.exclude for
    providers that DO honor the generic flag, this covers both mechanisms.

    Also logs latency + token usage + estimated cost to _telemetry_log --
    this is the "LLM economics" data the Round 2 brief asks the prototype
    to surface (model choice, token consumption, latency, cost per insight).
    """
    combined_system = "detailed thinking off\n\n" + system_prompt
    start = time.perf_counter()
    error = None
    usage = {"prompt_tokens": None, "completion_tokens": None, "total_tokens": None}
    text = ""
    try:
        response = client.chat.completions.create(
            model=model,
            max_tokens=1200,
            temperature=0,
            messages=[
                {"role": "system", "content": combined_system},
                {"role": "user", "content": user_prompt},
            ],
            extra_body={"reasoning": {"exclude": True}},
        )
        if not getattr(response, "choices", None):
            # Malformed/empty response -- happens under free-tier rate
            # limiting or provider overload; the request "succeeds" at the
            # HTTP layer but returns no actual completion. Surface this
            # clearly instead of crashing on response.choices[0].
            err_detail = getattr(response, "error", None) or "empty choices in response"
            raise RuntimeError(f"Model returned no completion ({err_detail}) -- "
                                f"likely rate-limited or overloaded.")
        text = response.choices[0].message.content or ""
        if getattr(response, "usage", None):
            usage = {
                "prompt_tokens": response.usage.prompt_tokens,
                "completion_tokens": response.usage.completion_tokens,
                "total_tokens": response.usage.total_tokens,
            }
    except Exception as e:
        error = str(e)
        raise
    finally:
        latency_ms = (time.perf_counter() - start) * 1000
        pricing = APPROX_PRICING_PER_1M_TOKENS.get(model, {"input": None, "output": None})
        est_cost = None
        if pricing["input"] is not None and usage["prompt_tokens"] is not None:
            est_cost = (usage["prompt_tokens"] / 1_000_000 * pricing["input"] +
                        (usage["completion_tokens"] or 0) / 1_000_000 * pricing["output"])
        _telemetry_log.append({
            "region": region, "week": week, "model": model,
            "latency_ms": round(latency_ms, 1),
            "prompt_tokens": usage["prompt_tokens"],
            "completion_tokens": usage["completion_tokens"],
            "total_tokens": usage["total_tokens"],
            "estimated_cost_usd": round(est_cost, 6) if est_cost is not None else 0.0,
            "success": error is None,
            "error": error,
        })
    return text


def _looks_like_leaked_reasoning(text):
    """Heuristic check for raw chain-of-thought leaking into the response
    despite the suppression attempts in _try_model -- catches outputs like
    'Here's a thinking process:' or numbered internal-analysis openers so
    we can escalate to the next candidate model instead of silently
    returning broken output."""
    head = text.strip()[:120].lower()
    leak_markers = ["thinking process", "let me think", "let's think",
                    "1.  **analyze", "1. **analyze", "i need to"]
    return any(marker in head for marker in leak_markers)


def call_llm(client, model, region, week, tickets):
    if not tickets:
        return {
            "summary": f"No price/competitor-related tickets found for {region} "
                       f"in week {week}. No customer signal to report here.",
            "confidence": "N/A",
            "n_tickets_summarized": 0,
        }

    ticket_block = "\n".join(f"- [{t['channel']}, {t['date']}] {t['text']}" for t in tickets)
    user_prompt = (
        f"Region: {region}\nWeek: {week}\n\n"
        f"Tickets flagged as price/competitor-related for this region-week:\n"
        f"{ticket_block}\n\n"
        f"Summarize what customers are saying."
    )

    # Try the cached "known good" model first as a fast path, but ALWAYS
    # keep the rest of the fallback chain available -- a model that worked
    # on a previous call can still fail transiently (rate limits, provider
    # overload) on this one, and we shouldn't dead-end just because it was
    # cached.
    ordered = []
    if "model" in _working_model_cache:
        ordered.append(_working_model_cache["model"])
    for m in [model] + FALLBACK_FREE_MODELS:
        if m not in ordered:
            ordered.append(m)
    candidates = ordered

    text = None
    last_error = None
    for candidate in candidates:
        try:
            attempt = _try_model(client, candidate, SYSTEM_PROMPT, user_prompt, region=region, week=week)
            if _looks_like_leaked_reasoning(attempt):
                print(f"  NOTE: '{candidate}' leaked raw chain-of-thought "
                      f"instead of a clean answer -- trying next candidate.")
                last_error = RuntimeError(f"{candidate} leaked chain-of-thought")
                continue
            text = attempt
            if candidate != model and "model" not in _working_model_cache:
                print(f"  NOTE: '{model}' unavailable/unsuitable, using '{candidate}' instead")
            _working_model_cache["model"] = candidate
            break
        except Exception as e:
            last_error = e
            continue

    if text is None:
        raise RuntimeError(
            f"All candidate models failed or leaked raw reasoning. Last error: "
            f"{last_error}\nCheck https://openrouter.ai/models?max_price=0 for "
            f"currently live free models and pass one explicitly with --model."
        )

    last_line = text.strip().upper().splitlines()[-1] if text.strip() else ""
    confidence = "HIGH" if "HIGH" in last_line else ("LOW" if "LOW" in last_line else "UNSTATED")

    return {
        "summary": text.strip(),
        "confidence": confidence,
        "n_tickets_summarized": len(tickets),
        "model_used": _working_model_cache.get("model", model),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--datadir", default=".")
    parser.add_argument("--model", default=DEFAULT_MODEL,
                         help=f"OpenRouter model ID (default: {DEFAULT_MODEL}, "
                              f"a free-tier model). Swap in any OpenRouter model, "
                              f"including paid Claude models, e.g. 'anthropic/claude-sonnet-4.5'")
    parser.add_argument("--dry-run", action="store_true",
                         help="Skip the API call and just show which tickets "
                              "would be sent per region-week (no API key needed).")
    args = parser.parse_args()
    d = Path(args.datadir)

    with open(d / "flagged_movements.json") as f:
        flags = json.load(f)

    if not (d / "customer_tickets.csv").exists():
        print("No customer_tickets.csv found -- Field has no source to read, skipping.")
        return

    ticket_rows = load_csv(d / "customer_tickets.csv")
    for r in ticket_rows:
        r["_week"] = week_from_date(r["date"])

    tickets_by_region_week = defaultdict(list)
    for r in ticket_rows:
        if classify_ticket(r["text"]):
            tickets_by_region_week[(r["region"], r["_week"])].append(r)

    # Field responds to flagged complaint_volume movements -- that's the KPI
    # this agent exists to explain.
    targets = []
    seen = set()
    for fl in flags:
        if fl["kpi_id"] != "complaint_volume":
            continue
        key = (fl["region"], fl["week"])
        if key in seen:
            continue
        seen.add(key)
        targets.append(key)

    if not targets:
        print("No complaint_volume movements flagged -- Field has nothing to investigate.")
        return

    client = None
    if not args.dry_run:
        try:
            from openai import OpenAI
        except ImportError:
            print("ERROR: the 'openai' package is required (OpenRouter uses the "
                  "OpenAI-compatible API format).")
            print("Install it with: pip install openai")
            print("...or run with --dry-run to see retrieval only, no API call.")
            sys.exit(1)

        api_key = os.environ.get("OPENROUTER_API_KEY")
        if not api_key:
            print("ERROR: OPENROUTER_API_KEY environment variable not set.")
            print("Get a free key at https://openrouter.ai -> Keys -> Create Key")
            print("Set it with (PowerShell): $env:OPENROUTER_API_KEY = 'sk-or-v1-...'")
            print("...or run with --dry-run to see retrieval only, no API call.")
            sys.exit(1)
        client = OpenAI(base_url="https://openrouter.ai/api/v1", api_key=api_key,
                         timeout=45.0, max_retries=0)
        print(f"Using OpenRouter model: {args.model} (45s timeout per call, no retries "
              f"-- slow/hung calls fail fast and fall through to the next candidate)\n")

    findings = []
    for region, week in targets:
        tickets = tickets_by_region_week.get((region, week), [])
        print(f"--- {region} week {week}: {len(tickets)} matching ticket(s) ---")
        for t in tickets:
            print(f"  [{t['channel']}] {t['text']}")

        if args.dry_run:
            result = {
                "summary": "(dry run -- no API call made)",
                "confidence": "N/A",
                "n_tickets_summarized": len(tickets),
            }
        else:
            result = call_llm(client, args.model, region, week, tickets)
            print(f"\n  Field summary: {result['summary']}\n")

        findings.append({
            "region": region, "week": week,
            "n_tickets_matched": len(tickets),
            **result,
        })

    out_path = d / "field_findings.json"
    with open(out_path, "w") as f:
        json.dump(findings, f, indent=2)
    print(f"\nField: {len(findings)} region-weeks investigated. Written to {out_path}")

    if _telemetry_log:
        successful = [t for t in _telemetry_log if t["success"]]
        total_latency = sum(t["latency_ms"] for t in _telemetry_log)
        total_tokens = sum(t["total_tokens"] or 0 for t in successful)
        total_cost = sum(t["estimated_cost_usd"] for t in successful)
        telemetry_summary = {
            "calls": _telemetry_log,
            "summary": {
                "total_calls": len(_telemetry_log),
                "successful_calls": len(successful),
                "failed_or_rejected_calls": len(_telemetry_log) - len(successful),
                "total_latency_ms": round(total_latency, 1),
                "avg_latency_ms": round(total_latency / len(_telemetry_log), 1),
                "total_tokens": total_tokens,
                "total_estimated_cost_usd": round(total_cost, 6),
                "note": "Cost figures use illustrative pricing (see "
                        "APPROX_PRICING_PER_1M_TOKENS) -- free-tier models "
                        "used here are genuinely $0; the paid-model rate is "
                        "for comparison only and should be checked against "
                        "current OpenRouter pricing before citing in a report.",
            },
        }
        telemetry_path = d / "telemetry.json"
        with open(telemetry_path, "w") as f:
            json.dump(telemetry_summary, f, indent=2)
        s = telemetry_summary["summary"]
        print(f"\nTelemetry: {s['total_calls']} LLM call attempt(s) "
              f"({s['successful_calls']} succeeded), "
              f"{s['total_latency_ms']:.0f}ms total, "
              f"{s['total_tokens']} tokens, "
              f"${s['total_estimated_cost_usd']:.6f} estimated cost. "
              f"Written to {telemetry_path}")


if __name__ == "__main__":
    main()
