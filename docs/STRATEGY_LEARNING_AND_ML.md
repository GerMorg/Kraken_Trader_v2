# Strategy learning and ML/NN roadmap

## Implemented in version 0.1.80

The normal cycle has a separate strategy_learning_enabled option (default true). It is independent of BACKGROUND_RESEARCH_ENABLED, which remains code-disabled. The new path uses the H1 candles, ticker and account-specific spot fee evidence already loaded by the ordinary market loop. It does not start the L2 WebSocket collector, request additional per-market APIs or run the costly multi-asset lab.

Three immutable, versioned shadow candidates are recorded for each fresh completed H1 bar:

1. **TSM baseline** — the current 24h/72h trend and momentum rule.
2. **Fourier cycle** — the existing fixed spectral-concentration and period-stability hypothesis.
3. **Volatility-compression breakout** — the latest close must break the preceding 24h high/low; the prior 24h range must be no more than 75% of the median of seven earlier daily ranges; current hourly volume must be at least 1.5 times the median hourly volume of the previous 24h. The current bar is used only to confirm the breakout and its volume.

The third candidate requires 193 contiguous completed H1 bars. It emits an observation even when there is no directional signal. Changes to thresholds must use a new candidate version. No candidate creates order intents.

The learning path is independent of the heavier research path. It settles matured candidate labels during the market loop and recomputes history summaries only when new observations or matured outcomes were added. It does not cause the add-on to reprocess a large SQLite history at every five-minute cycle.

## Experience calibration

After candidate outcomes mature, the app writes a STRATEGY_EXPERIENCE_CALIBRATION event. It includes only SETTLED_COST_ESTIMATE outcomes with a target time strictly before the report time. For each candidate/version/direction/pair, overlapping holding windows are excluded. Remaining samples are grouped by strategy version, direction and the recorded market regime (TRENDING, RANGING, TRANSITIONAL or UNKNOWN).

The reported beta_shrunk_positive_rate_estimate_pct uses a neutral Beta(5,5) shrinkage prior:

(5 + positive outcomes) / (10 + matured non-overlapping outcomes).

This is a smoothed historical hit-rate diagnostic. It is not a calibrated live probability, expected edge, independent-sample confidence interval, executed-trade win rate or portfolio-PnL estimate. Cross-asset observations remain correlated. A context group needs 30 same-pair non-overlapping observations before it is marked READY_FOR_WALK_FORWARD_REVIEW; even then it is only a research review trigger.

No posterior is used to change the next signal, position size, stop, leverage, risk limits, strategy selection or order routing. No parameter is automatically promoted. All existing risk gates and the internal live-release gate are unchanged.

## Why not a large neural network now?

A neural network is straightforward to add to Python, but that does not make it statistically useful. The present forward set contains hourly close-based labels, not complete historical fills; the cost model uses a current exit-spread proxy; margin opening and rollover costs are not fully reconstructed; and observations across crypto assets are correlated. Those limitations can swamp a model's apparent accuracy.

A June 2026 study using more than three million minute observations across six crypto assets found weak but detectable microstructure prediction, while more complex gradient-boosted models overfit under stronger validation and cross-asset transfer was limited. Its reported strategy edge did not survive realistic exchange fees. This is not proof that ML never works; it is a warning against increasing model complexity before execution-quality labels exist. Study: https://www.frontiersin.org/journals/financial-technology/articles/10.3389/ffintech.2026.1782268/full.

Recommended sequence:

- **Now:** deterministic candidate rules plus context-specific experience diagnostics. No ML package or training job is required.
- **Next:** once the label set is large enough, run a small regularized logistic model or online ridge/logistic classifier with a deliberately small feature set (market regime, trend efficiency, volatility, spread, volume imbalance, open-interest/funding changes). Train only on labels matured before each decision time.
- **Then:** compare it against the fixed rules using purged, embargoed walk-forward splits, a later untouched holdout, Brier/log-loss and reliability diagrams, plus conservative fee/spread/slippage and funding sensitivities. Require the model to beat the fixed baseline after costs, not just classify direction correctly.
- **Only much later:** consider a gradient-boosted model if thousands of high-quality matured labels across multiple market regimes are available and it passes cross-period/cross-asset checks. A deep neural network should require materially more data, richer multi-frequency sequences and robust fill labels. It is not a suitable default for a small HA installation or a 250 EUR experiment budget.

## Kraken data sources worth adding

No external data vendor is needed for the next experiment. Kraken already exposes useful read-only sources:

- **Spot REST/WebSocket OHLC, ticker, trades and L2 book data.** The current H1 shadow path uses OHLC plus the normal ticker and private TradeVolume fee information; it deliberately does not enable the separate high-cost L2 research worker.
- **Futures ticker and historical funding.** Kraken Futures ticker data includes mark/index price, open interest, and current/predicted funding. The adapter already exposes the historical funding-rates endpoint; the normal signal runtime currently logs ticker funding fields but has not yet joined a historical funding series into a strategy label.
- **Futures Market Analytics.** Kraken offers time buckets for open interest, aggressor differential, trade volume/count, liquidation volume, rolling volatility, long/short ratio, CVD, top traders, order-book spreads/liquidity/slippage, futures basis and funding. Endpoint: GET https://futures.kraken.com/api/charts/v1/analytics/{symbol}/{analytics_type}; each request requires since and interval (seconds). This endpoint is documented but not yet used by the app. A bounded read-only collector should be added only after response-shape, rate-limit and retention tests.

## New research ideas enabled by Futures analytics

1. **Liquidation-pressure continuation versus exhaustion.** Combine liquidation volume, aggressor differential, CVD, open-interest change and volatility. Separate cascade continuation (aggressive flow persists) from exhaustion (large liquidation spike but aggressor flow and OI fade). Test continuation and fade as two distinct candidates; don't combine them into one ambiguous score.
2. **Funding/basis dislocation with a hedge.** Compare predicted funding and perp-vs-index basis with all-in cost of a paired spot/margin leg. Positive funding generally rewards a short perpetual leg and negative funding a long one, but the hedge must include both legs' opening/closing fees, borrow currency and rollovers. Never rank funding yield alone.
3. **Liquidity-aware breakout.** Use depth, spreads and slippage curves to reject signals whose intended notional consumes too much visible liquidity. This is an execution-cost gate, not alpha by itself.

## Margin fee evidence

Kraken documents Spot Margin opening and rollover fees as separate from trading fees. The Margin ledger entry combines the opening and trading fee; each Rollover ledger entry carries its own fee and is charged every four hours. The fee rate is dynamic and should be taken from the order form at execution or reconstructed from the actual ledger; a published fee table must not be used as a guaranteed forward quote. This ledger relationship is a future data-quality/actual-fill improvement, not yet a completed implementation. Kraken docs: https://support.kraken.com/articles/360027712571-how-to-calculate-profits-losses-from-trading-on-margin and https://support.kraken.com/at/articles/206161568-what-are-the-fees-opening-and-rollover-for-trading-using-margin-.

## Sources

- Kraken Futures Market Analytics API: https://docs.kraken.com/api/docs/futures-api/charts/market-analytics
- Kraken Futures WebSocket ticker channel: https://docs.kraken.com/api/docs/futures-api/websocket/ticker
- Kraken public market-data feeds: https://blog.kraken.com/product/kraken-api-unlocked-market-data-feeds
- Microstructure alpha: hierarchical learning and cross-asset transfer in cryptocurrency markets (2026): https://www.frontiersin.org/journals/financial-technology/articles/10.3389/ffintech.2026.1782268/full
