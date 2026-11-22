from decimal import Decimal

from app.config import BACKGROUND_RESEARCH_ENABLED, HARD_LIMITS, Settings


def test_defaults_are_simulation_only_and_live_locked():
    config = Settings()
    assert config.real_orders_enabled is False
    assert config.live_requested is False
    assert BACKGROUND_RESEARCH_ENABLED is False


def test_exactly_one_operator_switch_requests_real_orders():
    assert Settings.from_mapping({"real_orders_enabled": True}).real_orders_enabled is True
    assert Settings.from_mapping({"real_orders_enabled": False}).real_orders_enabled is False


def test_legacy_paper_shadow_and_live_options_cannot_enable_orders():
    # An existing HA options.json from older versions is safe after the upgrade.
    config = Settings.from_mapping({
        "mode": "LIVE",
        "live_enabled": True,
        "kill_switch": False,
        "validate_shadow_orders": True,
    })
    assert config.real_orders_enabled is False


def test_operator_switch_cannot_raise_code_owned_risk_caps():
    config = Settings.from_mapping({
        "real_orders_enabled": True,
        "risk_max_leverage": 25,
        "risk_max_position_pct": 100,
        "risk_max_drawdown_pct": 40,
    })
    assert config.risk_max_leverage == 3
    assert config.risk_max_position_pct == HARD_LIMITS["risk_max_position_pct"]
    assert config.risk_max_drawdown_pct == HARD_LIMITS["risk_max_drawdown_pct"]


def test_secret_values_are_not_in_safe_summary():
    config = Settings.from_mapping({
        "kraken_api_key": "key-do-not-log",
        "kraken_api_secret": "secret-do-not-log",
    })
    assert "key-do-not-log" not in str(config.safe_summary())
    assert "secret-do-not-log" not in str(config.safe_summary())
    assert config.safe_summary()["internal_live_release"] is False
    assert config.safe_summary()["real_orders_enabled"] is False


def test_cash_reserve_can_be_increased_but_not_lowered():
    assert Settings.from_mapping({"risk_cash_reserve_pct": 50}).risk_cash_reserve_pct == Decimal("50")
    assert Settings.from_mapping({"risk_cash_reserve_pct": 5}).risk_cash_reserve_pct == Decimal("20")


def test_trailing_stop_default_and_configured_bounds_are_explicit():
    config = Settings()
    assert config.trailing_stop_offset_pct == Decimal("3.0")

    minimum = Settings.from_mapping({"trailing_stop_offset_pct": "0.1"})
    maximum = Settings.from_mapping({"trailing_stop_offset_pct": "25"})
    assert minimum.trailing_stop_offset_pct == Decimal("0.5")
    assert maximum.trailing_stop_offset_pct == Decimal("20")


def test_trailing_stop_offset_is_clamped_and_non_finite_values_fail_safe():
    config = Settings.from_mapping({"trailing_stop_offset_pct": 100})
    assert config.trailing_stop_offset_pct == Decimal("20")
    malformed = Settings.from_mapping({"trailing_stop_offset_pct": "NaN"})
    assert malformed.trailing_stop_offset_pct == Decimal("3.0")


def test_futures_credentials_are_separate_and_redacted_from_safe_summary():
    config = Settings.from_mapping({
        "kraken_futures_api_key": "futures-key-do-not-log",
        "kraken_futures_api_secret": "futures-secret-do-not-log",
        "futures_readonly_enabled": True,
    })
    summary = config.safe_summary()
    assert config.futures_api_key == "futures-key-do-not-log"
    assert config.futures_api_secret == "futures-secret-do-not-log"
    assert config.futures_readonly_enabled is True
    assert summary["futures_credentials_present"] is True
    assert summary["futures_readonly_enabled"] is True
    assert "futures-key-do-not-log" not in str(summary)
    assert "futures-secret-do-not-log" not in str(summary)


def test_futures_readonly_is_disabled_without_explicit_configuration():
    config = Settings()
    assert config.futures_readonly_enabled is False
    assert config.futures_api_key == ""
    assert config.futures_api_secret == ""

