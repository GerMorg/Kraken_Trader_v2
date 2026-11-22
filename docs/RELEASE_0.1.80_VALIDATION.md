# Release 0.1.80 — validation record

## Scope

- Independent strategy_learning_enabled option, default enabled.
- Three forward-only candidates: TSM, Fourier and volatility-compression breakout.
- Candidate collection and outcome settlement use the normal H1 market candles; the code-owned heavy background-research flag remains false.
- Beta(5,5) historical-rate shrinkage diagnostic from matured costed outcomes, excluding same-pair overlapping labels.
- No new third-party dependencies, database reset, account mutations, order intents, real orders, strategy promotion or live-gate change.

## Validation

Automated validation is provided by the GitHub Actions workflow for this release pull request and merge commit. The validation checklist is Ruff, mypy, the full pytest suite, Python compile, HA add-on structure, AppArmor policy syntax, container startup/build and dependency/security checks.

The run IDs and exact result counts are added after the pull request workflow completes. Until those checks pass, this change must not be merged to main.

## Operational verification after installation

1. Confirm strategy_learning_enabled: true in the startup configuration summary and CYCLE_SUMMARY.
2. Confirm SHADOW_STRATEGY_CANDIDATE_RECORDED events show the three candidate names. A no-direction observation is valid and must be retained.
3. After the configured holding horizon, confirm SHADOW_STRATEGY_CANDIDATE_OUTCOME_SETTLED reports SETTLED_COST_ESTIMATE only when the fee/spread cost proxy is available.
4. Confirm STRATEGY_EXPERIENCE_CALIBRATION excludes pending, gross-only, future-target and same-pair overlapping observations.
5. Confirm real_orders_enabled remains false unless deliberately operator-configured and that internal_live_release remains false regardless.

No result in this report is evidence of profitability or a real-fill simulation.
