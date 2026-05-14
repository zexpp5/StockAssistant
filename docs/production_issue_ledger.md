# Production Issue Ledger

Last updated: 2026-05-14

This ledger separates production logic defects from data/verification state and naming/documentation debt.

| Issue | Type | Status | Evidence | Next Verification |
|---|---|---|---|---|
| `avoid/watch` rows mixed into default production recommendations | Production logic | Fixed in code | `picks.signal` is written; quality/evidence default to `COALESCE(signal,'buy')='buy'` | Run full daily refresh and verify latest `v6_us/v6_hk/v6_cn` buy counts |
| Dashboard picks-only candidates dropped by `watchlist INNER JOIN` | Production logic | Fixed in code | `fetch_picks_view()` uses `LEFT JOIN watchlist` for review/latest picks | Rebuild dashboard and confirm picks-only rows render |
| US portfolio still using legacy naked Markowitz | Production logic | Fixed in code | `daily_refresh.sh` step 10 runs `stock_research.jobs.optimize_portfolio`; optimizer writes compatible `plan_a_v5.json` and `plan_v6.json` | Run optimizer and compare `method` contains `risk_aware_optimize` |
| IC gate did not cover production factors | Production logic | Fixed in code, needs data | Gate watches six production factors; unhealthy factors are zero-weighted when at least one factor is healthy | Run `audit_ic`; verify `factor_weights_used` in picks/plan |
| Missing factor data filled as neutral | Production logic | Fixed in code for US/HK/A-share | Composite now emits `coverage_score`, `missing_factors`, and applies coverage penalty | Verify latest picks have `coverage_score` populated |
| HK recommendations concentrated by sector | Production logic | Fixed in code | HK selected list applies a 35% sector cap before writing picks | Run HK picks and inspect skipped sector cap rows |
| API/manual picks rerun bypassed IC/audit gates by default | Production logic | Fixed in code | `/api/picks/rerun` now rejects bypass flags unless `allow_gate_bypass=true` and records bypass metadata in lock/log | Trigger rerun from API and verify command has no bypass flags |
| A-share reused US IC gate / heuristic weights entered production | Production logic | Fixed in code, blocks until calibrated | A-share requires a valid `data/calibrated_factor_weights.json` with `market=a_share`, `validated=true`, and weights summing to 1 unless `--bypass-ic-gate` is explicit | Generate calibrated weights or keep A-share as dry-run |
| `data/latest` incomplete or stale | Verification/data | Open | Current `data/latest` lacks full plan/trade/risk output until pipeline reruns | Run daily chain end to end |
| DuckDB/dashboard may contain old generated state | Verification/data | Open | Code changes do not rewrite historical DB rows or generated HTML automatically | Rebuild DB-derived outputs and dashboard |
| No one-shot production acceptance check | Verification/data | Fixed in code | `scripts/tools/production_acceptance_check.py` checks schema, latest artifacts, plan method, signal isolation, trade deltas, dashboard, and brief; `daily_refresh.sh` runs it as step 27 | Run acceptance after full refresh |
| Historical `plan_a_v5` naming remains | Naming/documentation | Accepted short-term | Old filename kept as compatibility path; payload method marks v6 risk-aware | Long-term migration to `plan_v6.json` readers |
