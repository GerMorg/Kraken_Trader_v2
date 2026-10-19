from decimal import Decimal

from app.config import HARD_LIMITS, Settings


def test_defaults_are_shadow_and_live_locked():
    config = Settings()
    assert config.mode == "SHADOW"
    assert config.kill_switch
    assert not config.live_requested


def test_options_cannot_raise_code_owned_caps():
    config = Settings.from_mapping({
        "mode": "LIVE", "live_enabled": True, "kill_switch": False,
        "risk_max_leverage": 25, "risk_max_position_pct": 100, "risk_max_drawdown_pct": 40,
    })
    assert config.risk_max_leverage == 3
    assert config.risk_max_position_pct == HARD_LIMITS["risk_max_position_pct"]
    assert config.risk_max_drawdown_pct == HARD_LIMITS["risk_max_drawdown_pct"]
    assert config.live_requested


def test_unknown_mode_falls_back_to_shadow():
    assert Settings.from_mapping({"mode": "unexpected"}).mode == "SHADOW"


def test_secret_values_are_not_in_safe_summary():
    config = Settings.from_mapping({
        "kraken_api_key": "key-do-not-log",
        "kraken_api_secret": "secret-do-not-log",
    })
    assert "key-do-not-log" not in str(config.safe_summary())
    assert "secret-do-not-log" not in str(config.safe_summary())
    assert config.safe_summary()["internal_live_release"] is False


def test_cash_reserve_can_be_increased_but_not_lowered():
    assert Settings.from_mapping({"risk_cash_reserve_pct": 50}).risk_cash_reserve_pct == Decimal("50")
    assert Settings.from_mapping({"risk_cash_reserve_pct": 5}).risk_cash_reserve_pct == Decimal("20")


def test_position_exit_policy_defaults_are_explicit_and_bounded():
    config = Settings()
    assert config.position_take_profit_pct == Decimal("3.5")
    assert config.position_take_profit_fraction == Decimal("0.5")
    assert config.position_stop_loss_pct == Decimal("3.0")


def test_position_exit_policy_settings_are_clamped_and_non_finite_values_fail_safe():
    config = Settings.from_mapping({
        "position_take_profit_pct": 100,
        "position_take_profit_fraction": 0,
        "position_stop_loss_pct": "NaN",
    })
    assert config.position_take_profit_pct == Decimal("20")
    assert config.position_take_profit_fraction == Decimal("0.1")
    # A malformed decimal follows the documented configured default.
    assert config.position_stop_loss_pct == Decimal("3.0")
    assert config.safe_summary()["position_take_profit_pct"] == "20"
