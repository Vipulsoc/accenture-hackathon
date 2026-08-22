# Quorum Prototype - Synthetic Data Layer

Scenario: BizzGrow Inc., 3 regions (APAC, EMEA, NA) + 1 newly-launched sub-region
(APAC-South), 12 weeks of data (2026-06-01 to 2026-08-17).

## Files

| File | Source system (simulated) | Grain | Cadence |
|---|---|---|---|
| `sales_weekly.csv` | Sales DB | region-week | weekly |
| `ops_events.csv` | Ops/change log | event | event-driven |
| `customer_tickets.csv` | Support/CRM (unstructured text) | ticket | near-real-time |
| `deals.csv` | CRM | deal | daily |
| `marketing_spend.csv` | Marketing platform | region-week | weekly |
| `kpi_contracts.json` | Semantic layer (hand-authored) | per-KPI | n/a |

## The story baked into the data

- **APAC revenue drops ~16.8% between week 7 and week 9** (verified — flat in
  EMEA/NA over the same window), driven by two overlapping causes:
  1. A 6% price increase rolled out in week 8 (`ops_events.csv` OPS-101)
  2. A competitor ("NimbusCo") undercutting on enterprise renewals from week 9
     (`ops_events.csv` OPS-102, `deals.csv` loss reasons, `customer_tickets.csv`)
  - `deals.csv` has 12 APAC deals closing weeks 9-10: 8 cite price, 5 cite the
    competitor, 3 cite both — this is your "not yet separable, check the 12
    lost deals" moment from the original deck, now with real rows to point at.

- **Sparse-history KPI**: `APAC-South` in `sales_weekly.csv` only has 4 weeks
  of data (launched week 9) — Pattern agent should report no reliable
  historical analog here rather than forcing a match.

- **Low-confidence / abstention case**: EMEA has an elevated ticket count in
  `customer_tickets.csv`, but the tickets are about a resolved billing portal
  outage, not price or competitor sensitivity — and EMEA revenue is flat.
  This is a deliberate trap for the Falsifier: Field agent might flag EMEA
  tickets as anomalous, but Statistician shows no matching revenue movement.
  Correct system behavior is to **not** merge this into the APAC narrative.

- **Role-based security scenario**: `kpi_contracts.json` tags every KPI with
  row-level access by region. Simulate an EMEA-scoped
  `regional_sales_manager` querying APAC data being denied/masked, logged
  for audit.

## Maps to Round 2 minimum prototype checklist

- [x] 5 connected KPIs across 3 source types with different grains/cadences
- [x] Lightweight KPI/semantic contract (`kpi_contracts.json`)
- [x] Multi-factor KPI movement with known simulated drivers (APAC week 9)
- [x] Low-confidence/abstention scenario (EMEA tickets vs. flat revenue)
- [x] Sparse-history KPI scenario (APAC-South)
- [x] Role-based security scenario (row-level tags + masking test)

## Next steps (not yet built)

1. Detection layer: rolling-window z-score / % change script over
   `sales_weekly.csv` using each KPI's `materiality_threshold` to auto-flag
   the APAC week-9 movement (non-LLM).
2. Four specialist agents reading from their respective files.
3. Falsifier/Arbiter logic with a simple accuracy-weight table.
4. Persona rendering (CFO vs. Regional Sales Manager) off the same verdict object.
