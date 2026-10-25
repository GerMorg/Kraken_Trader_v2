#!/usr/bin/env python3
"""Run the offline, research-only strategy comparison on a Kraken OHLCVT CSV."""

from __future__ import annotations

import argparse
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
import sys
from typing import Sequence

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "kraken_margin_trader"))

from app.strategy_lab import (  # noqa: E402
    SUPPORTED_INTERVAL_MINUTES,
    compare_strategies,
    load_kraken_ohlcvt,
)


def _decimal_argument(raw: str) -> Decimal:
    try:
        return Decimal(raw)
    except InvalidOperation as exc:
        raise argparse.ArgumentTypeError("must be a decimal number") from exc


def _margin_budgets_argument(raw: str) -> list[Decimal]:
    parts = [part.strip() for part in raw.split(",")]
    if not parts or any(not part for part in parts):
        raise argparse.ArgumentTypeError("provide comma-separated bps values, e.g. 5,10,20,40")
    try:
        values = [Decimal(part) for part in parts]
    except InvalidOperation as exc:
        raise argparse.ArgumentTypeError("all margin budgets must be decimal numbers") from exc
    if any(not value.is_finite() or value < 0 for value in values):
        raise argparse.ArgumentTypeError("margin budgets must be finite and non-negative")
    if len(set(values)) != len(values):
        raise argparse.ArgumentTypeError("margin budgets must be unique")
    return values


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Compare fixed TSM, mean-reversion, and logistic-regression research candidates. "
            "This command never creates orders or changes the live trading database."
        )
    )
    parser.add_argument("--input", required=True, type=Path, help="Headerless Kraken OHLCVT CSV")
    parser.add_argument("--pair", required=True, help="Exact Kraken pair label for the report")
    parser.add_argument(
        "--interval-minutes",
        required=True,
        type=int,
        choices=sorted(SUPPORTED_INTERVAL_MINUTES),
        help="Input candle interval; this initial strategy comparison requires 60",
    )
    parser.add_argument(
        "--taker-fee-bps-per-side",
        required=True,
        type=_decimal_argument,
        help="User-verified taker fee tier in bps per side; not inferred by this tool",
    )
    parser.add_argument(
        "--spread-bps-round-trip",
        required=True,
        type=_decimal_argument,
        help="Assumed round-trip spread cost in bps",
    )
    parser.add_argument(
        "--slippage-bps-per-side",
        required=True,
        type=_decimal_argument,
        help="Assumed slippage in bps per side",
    )
    parser.add_argument(
        "--margin-cost-budgets-bps",
        required=True,
        type=_margin_budgets_argument,
        help="Sensitivity assumptions for combined margin costs, e.g. 5,10,20,40; not Kraken quotes",
    )
    parser.add_argument(
        "--holding-bars",
        type=int,
        default=8,
        help="Fixed holding horizon in candles (default: 8)",
    )
    parser.add_argument(
        "--fast-threshold-bps",
        type=_decimal_argument,
        default=Decimal("120"),
        help="Current TSM fast trend threshold in bps (default: 120)",
    )
    parser.add_argument(
        "--slow-threshold-bps",
        type=_decimal_argument,
        default=Decimal("180"),
        help="Current TSM slow trend threshold in bps (default: 180)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Optional JSON report path outside the live database and runtime",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        candles, metadata = load_kraken_ohlcvt(
            args.input, interval_minutes=args.interval_minutes
        )
        report = compare_strategies(
            candles,
            pair=args.pair,
            interval_minutes=args.interval_minutes,
            taker_fee_bps_per_side=args.taker_fee_bps_per_side,
            spread_bps_round_trip=args.spread_bps_round_trip,
            slippage_bps_per_side=args.slippage_bps_per_side,
            margin_cost_budgets_bps=args.margin_cost_budgets_bps,
            holding_bars=args.holding_bars,
            fast_threshold_bps=args.fast_threshold_bps,
            slow_threshold_bps=args.slow_threshold_bps,
            source_metadata=metadata,
        )
    except (OSError, ValueError) as exc:
        parser.error(str(exc))

    rendered = json.dumps(report, indent=2, sort_keys=True, allow_nan=False)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
        print(
            json.dumps(
                {
                    "event": "STRATEGY_LAB_REPORT_WRITTEN",
                    "path": str(args.output.resolve()),
                    "status": report["status"],
                    "release_gate_eligible": report["release_gate_eligible"],
                },
                sort_keys=True,
            )
        )
    else:
        print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
