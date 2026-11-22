# Release 0.1.80 — validation record

## Scope

- Independent strategy_learning_enabled option, default enabled.
- Three forward-only candidates: TSM, Fourier and volatility-compression breakout.
- Candidate collection and outcome settlement use the normal H1 market candles; the code-owned heavy background-research flag remains false.
- Beta(5,5) historical-rate shrinkage diagnostic from matured costed outcomes, excluding same-pair overlapping labels.
- No new third-party dependencies, database reset, account mutations, order intents, real orders, strategy promotion or live-gate change.

## Validation

The initial complete validation of code commit 544e7c02b23a2043def375860bb61c9ab318890f passed in GitHub Actions run 45064376449:

- Ruff: all checks passed.
- mypy: no issues found in 32 source files.
- pytest: 503 passed.
- Home Assistant add-on structure and version validation: passed.
- Python compile: passed.
- AppArmor profile syntax and policy: passed.
- Container build and startup checks: passed.
- Security and dependency audit: passed.

This release record is part of the pull request, so CI is rerun against the final documentation commit as well. Merge only when that final run is green.

## Operational verification after installation

1. Confirm strategy_learning_enabled: true in the startup configuration summary and CYCLE_SUMMARY.
2. Confirm SHADOW_STRATEGY_CANDIDATE_RECORDED events show the three candidate names. A no-direction observation is valid and must be retained.
3. After the configured holding horizon, confirm SHADOW_STRATEGY_CANDIDATE_OUTCOME_SETTLED reports SETTLED_COST_ESTIMATE only when the fee/spread cost proxy is available.
4. Confirm STRATEGY_EXPERIENCE_CALIBRATION excludes pending, gross-only, future-target and same-pair overlapping observations.
5. Confirm real_orders_enabled remains false unless deliberately operator-configured and that internal_live_release remains false regardless.

No result in this report is evidence of profitability or a real-fill simulation.
