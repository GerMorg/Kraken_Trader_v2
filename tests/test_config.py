from decimal import Decimal

from app.config import BACKGROUND_RESEARCH_ENABLED, Settings


def test_defaults_are_simulation_only_and_live_locked():
    config = Settings()
    assert config.real_orders_enabled is False
    assert config.live_requested is False
    assert BACKGROUND_RESEARCH_ENABLED is False
    assert config.risk_min_margin_level_pct == Decimal("300")


def test_exactly_one_operator_switch_requests_real_orders():
    assert Settings.from_mapping({"real_orders_enabled": True}).real_orders_enabled is True
    assert Settings.from_mapping({"real_orders_enabled": False}).real_orders_enabled is False


def test_legacy_paper_shadow_and_live_options_cannot_enable_orders():
    config = Settings.from_mapping({
        "mode": "LIVE",
        "live_enabled": True,
        "kill_switch": False,
        "validate_shadow_orders": True,
    })
    assert config.real_orders_enabled is False


def test_operator_can_configure_risk_budgets_without_app_owned_upper_caps():
    config = Settings.from_mapping({
        "risk_per_trade_pct": "2.5",
        "risk_max_position_pct": "125",
        "risk_max_gross_pct": "350",
        "risk_max_net_pct": "240",
        "risk_max_margin_pct": "110",
        "risk_max_leverage": 25,
        "risk_max_open_positions": 12,
        "risk_daily_loss_pct": "15",
        "risk_max_drawdown_pct": "40",
    })
    assert config.risk_per_trade_pct == Decimal("2.5")
    assert config.risk_max_position_pct == Decimal("125")
    assert config.risk_max_gross_pct == Decimal("350")
    assert config.risk_max_net_pct == Decimal("240")
    assert config.risk_max_margin_pct == Decimal("110")
    assert config.risk_max_leverage == 25
    assert config.risk_max_open_positions == 12
    assert config.risk_daily_loss_pct == Decimal("15")
    assert config.risk_max_drawdown_pct == Decimal("40")
    summary = config.safe_summary()
    assert summary["risk_max_leverage"] == 25
    assert summary["risk_max_open_positions"] == 12


def test_zero_risk_ceilings_can_intentionally_block_new_entries():
    config = Settings.from_mapping({
        "risk_per_trade_pct": 0,
        "risk_max_position_pct": 0,
        "risk_max_gross_pct": 0,
        "risk_max_net_pct": 0,
        "risk_max_margin_pct": 0,
        "risk_max_open_positions": 0,
        "risk_min_margin_level_pct": 0,
    })
    assert config.risk_per_trade_pct == Decimal("0")
    assert config.risk_max_position_pct == Decimal("0")
    assert config.risk_max_gross_pct == Decimal("0")
    assert config.risk_max_net_pct == Decimal("0")
    assert config.risk_max_margin_pct == Decimal("0")
    assert config.risk_max_open_positions == 0
    assert config.risk_min_margin_level_pct == Decimal("0")


def test_invalid_risk_limits_fall_back_to_defaults():
    config = Settings.from_mapping({
        "risk_per_trade_pct": "-1",
        "risk_max_position_pct": "NaN",
        "risk_max_gross_pct": "Infinity",
        "risk_max_net_pct": "-10",
        "risk_max_margin_pct": "bad",
        "risk_max_leverage": 0,
        "risk_max_open_positions": -1,
        "risk_daily_loss_pct": "-1",
        "risk_max_drawdown_pct": "NaN",
        "risk_min_margin_level_pct": "-1",
    })
    defaults = Settings()
    assert config.risk_per_trade_pct == defaults.risk_per_trade_pct
    assert config.risk_max_position_pct == defaults.risk_max_position_pct
    assert config.risk_max_gross_pct == defaults.risk_max_gross_pct
    assert config.risk_max_net_pct == defaults.risk_max_net_pct
    assert config.risk_max_margin_pct == defaults.risk_max_margin_pct
    assert config.risk_max_leverage == defaults.risk_max_leverage
    assert config.risk_max_open_positions == defaults.risk_max_open_positions
    assert config.risk_daily_loss_pct == defaults.risk_daily_loss_pct
    assert config.risk_max_drawdown_pct == defaults.risk_max_drawdown_pct
    assert config.risk_min_margin_level_pct == defaults.risk_min_margin_level_pct


def test_cash_reserve_is_configurable_across_its_natural_zero_to_100_range():
    assert Settings.from_mapping({"risk_cash_reserve_pct": 0}).risk_cash_reserve_pct == Decimal("0")
    assert Settings.from_mapping({"risk_cash_reserve_pct": 50}).risk_cash_reserve_pct == Decimal("50")
    assert Settings.from_mapping({"risk_cash_reserve_pct": 100}).risk_cash_reserve_pct == Decimal("100")
    assert Settings.from_mapping({"risk_cash_reserve_pct": -1}).risk_cash_reserve_pct == Decimal("20")
    assert Settings.from_mapping({"risk_cash_reserve_pct": 101}).risk_cash_reserve_pct == Decimal("20")


def test_strategy_and_holding_parameters_are_not_limited_by_arbitrary_app_caps():
    config = Settings.from_mapping({
        "strategy_fast_bps": "2500",
        "strategy_slow_bps": "4000",
        "trailing_stop_offset_pct": "75",
        "expected_holding_hours": 2000,
    })
    assert config.strategy_fast_bps == Decimal("2500")
    assert config.strategy_slow_bps == Decimal("4000")
    assert config.trailing_stop_offset_pct == Decimal("75")
    assert config.expected_holding_hours == 2000


def test_invalid_strategy_values_fall_back_to_defaults():
    config = Settings.from_mapping({
        "strategy_fast_bps": "NaN",
        "strategy_slow_bps": "-1",
        "trailing_stop_offset_pct": "Infinity",
        "expected_holding_hours": 0,
    })
    defaults = Settings()
    assert config.strategy_fast_bps == defaults.strategy_fast_bps
    assert config.strategy_slow_bps == defaults.strategy_slow_bps
    assert config.trailing_stop_offset_pct == defaults.trailing_stop_offset_pct
    assert config.expected_holding_hours == defaults.expected_holding_hours


def test_secret_values_are_not_in_safe_summary():
    config = Settings.from_mapping({
        "kraken_api_key": "key-do-not-log",
        "kraken_api_secret": "secret-do-not-log",
        "kraken_futures_api_key": "futures-key-do-not-log",
        "kraken_futures_api_secret": "futures-secret-do-not-log",
    })
    summary = config.safe_summary()
    assert "key-do-not-log" not in str(summary)
    assert "secret-do-not-log" not in str(summary)
    assert "futures-key-do-not-log" not in str(summary)
    assert "futures-secret-do-not-log" not in str(summary)
    assert summary["internal_live_release"] is False
    assert summary["real_orders_enabled"] is False
    assert summary["futures_credentials_present"] is True


def test_futures_readonly_is_disabled_without_explicit_configuration():
    config = Settings()
    assert config.futures_readonly_enabled is False
    assert config.futures_api_key == ""
    assert config.futures_api_secret == ""


def test_retired_demo_options_are_ignored_and_not_exposed_in_runtime_settings():
    config = Settings.from_mapping({
        "kraken_futures_demo_api_key": "obsolete-demo-key",
        "kraken_futures_demo_api_secret": "obsolete-demo-secret",
        "futures_demo_enabled": True,
    })
    assert "futures_demo_enabled" not in config.safe_summary()
    assert "obsolete-demo-key" not in str(config.safe_summary())
    assert "obsolete-demo-secret" not in str(config.safe_summary())


def test_daily_loss_and_drawdown_limits_remain_operator_controlled_without_upper_caps():
    config = Settings.from_mapping({
        "risk_daily_loss_pct": "12.5",
        "risk_max_drawdown_pct": "27.5",
    })
    assert config.risk_daily_loss_pct == Decimal("12.5")
    assert config.risk_max_drawdown_pct == Decimal("27.5")
    assert config.safe_summary()["risk_daily_loss_pct"] == "12.5"
    assert config.safe_summary()["risk_max_drawdown_pct"] == "27.5"


def test_minimum_margin_level_is_operator_configurable():
    config = Settings.from_mapping({"risk_min_margin_level_pct": "650"})
    assert config.risk_min_margin_level_pct == Decimal("650")
    assert config.safe_summary()["risk_min_margin_level_pct"] == "650"


def test_strategy_efficiency_and_stop_thresholds_are_operator_configurable():
    config = Settings.from_mapping({
        "strategy_fast_efficiency_min": "0.05",
        "strategy_slow_efficiency_min": "0.01",
        "strategy_stop_min_pct": "0.25",
        "strategy_stop_max_pct": "12.5",
    })
    assert config.strategy_fast_efficiency_min == Decimal("0.05")
    assert config.strategy_slow_efficiency_min == Decimal("0.01")
    assert config.strategy_stop_min_pct == Decimal("0.25")
    assert config.strategy_stop_max_pct == Decimal("12.5")
    assert config.safe_summary()["strategy_stop_max_pct"] == "12.5"


def test_invalid_or_inverted_strategy_thresholds_fall_back_to_defaults():
    config = Settings.from_mapping({
        "strategy_fast_efficiency_min": "1.1",
        "strategy_slow_efficiency_min": "-0.1",
        "strategy_stop_min_pct": "10",
        "strategy_stop_max_pct": "5",
    })
    defaults = Settings()
    assert config.strategy_fast_efficiency_min == defaults.strategy_fast_efficiency_min
    assert config.strategy_slow_efficiency_min == defaults.strategy_slow_efficiency_min
    assert config.strategy_stop_min_pct == defaults.strategy_stop_min_pct
    assert config.strategy_stop_max_pct == defaults.strategy_stop_max_pct


def test_operational_polling_and_market_scan_controls_have_no_app_owned_upper_caps():
    config = Settings.from_mapping({
        "cycle_interval_seconds": 86400,
        "max_market_candidates": 200,
        "market_max_age_seconds": 86400,
    })
    assert config.cycle_interval_seconds == 86400
    assert config.max_market_candidates == 200
    assert config.market_max_age_seconds == 86400


def test_operational_controls_keep_only_minimums_needed_for_api_and_data_quality():
    config = Settings.from_mapping({
        "cycle_interval_seconds": 10,
        "max_market_candidates": 0,
        "market_max_age_seconds": 2,
    })
    assert config.cycle_interval_seconds == 60
    assert config.max_market_candidates == 12
    assert config.market_max_age_seconds == 15


def test_futures_strategy_and_market_count_are_separately_configurable():
    config = Settings.from_mapping({
        "futures_max_market_candidates": 80,
        "futures_strategy_fast_bps": "2400",
        "futures_strategy_slow_bps": "3600",
    })
    assert config.futures_max_market_candidates == 80
    assert config.futures_strategy_fast_bps == Decimal("2400")
    assert config.futures_strategy_slow_bps == Decimal("3600")


def test_invalid_futures_strategy_settings_fall_back_to_defaults():
    config = Settings.from_mapping({
        "futures_max_market_candidates": 0,
        "futures_strategy_fast_bps": "NaN",
        "futures_strategy_slow_bps": "-1",
    })
    defaults = Settings()
    assert config.futures_max_market_candidates == defaults.futures_max_market_candidates
    assert config.futures_strategy_fast_bps == defaults.futures_strategy_fast_bps
    assert config.futures_strategy_slow_bps == defaults.futures_strategy_slow_bps
