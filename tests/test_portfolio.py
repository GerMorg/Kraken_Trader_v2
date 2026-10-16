from decimal import Decimal
import pytest

from app.portfolio import (
    aggregate_position_exposure,
    expected_eea_funding_currency,
    parse_open_margin_positions,
    resolve_close_quantity,
)

D=Decimal

def test_close_cannot_consume_unassigned_wallet_inventory():
    with pytest.raises(ValueError,match="EXCEEDS_VERIFIED"):
        resolve_close_quantity(requested=D("10"),tracked_pair_quantity=D("8"),
           free_wallet_quantity=D("100"),product_type="CASH_SPOT")
    assert resolve_close_quantity(requested=D("8"),tracked_pair_quantity=D("8"),
           free_wallet_quantity=D("100"),product_type="CASH_SPOT")==D("8")

def test_open_margin_position_volume_is_less_closed_volume():
    raw={"TX":{"posstatus":"open","pair":"XXBTZUSD","type":"sell","vol":"1",
      "vol_closed":"0.25","cost":"50000","fee":"10","margin":"10000",
      "time":"100","rollovertm":"200","terms":"dynamic"}}
    rows=parse_open_margin_positions(raw)
    assert len(rows)==1 and rows[0].side=="SHORT" and rows[0].remaining_volume==D("0.75")

def test_eea_funding_label_is_policy_expectation_not_account_proof():
    assert expected_eea_funding_currency("USD")=="USDC"
    assert expected_eea_funding_currency("EUR")=="EURC"
    assert expected_eea_funding_currency("GBP") is None

def test_unknown_product_type_is_rejected():
    with pytest.raises(ValueError,match="UNKNOWN_PRODUCT"):
        resolve_close_quantity(requested=D("1"),tracked_pair_quantity=D("1"),
           free_wallet_quantity=D("1"),product_type="FUTURES")


def test_position_exposure_uses_direction_not_exchange_value_sign():
    exposure = aggregate_position_exposure({
        "LONG": {"type": "buy", "value": "100"},
        "SHORT": {"type": "sell", "value": "-30"},
    })
    assert exposure.complete
    assert exposure.gross_usd == D("130")
    assert exposure.net_usd == D("70")


def test_position_exposure_fails_closed_on_missing_or_invalid_value():
    missing = aggregate_position_exposure({"LONG": {"type": "buy"}})
    invalid = aggregate_position_exposure({"SHORT": {"type": "sell", "value": "NaN"}})
    assert not missing.complete
    assert not invalid.complete


def test_invalid_decimal_margin_position_is_ignored_without_sort_failure():
    rows = parse_open_margin_positions({
        "VALID": {"posstatus": "open", "pair": "XBTUSD", "type": "buy", "vol": "1",
                  "vol_closed": "0", "cost": "100", "fee": "0", "margin": "50", "time": "1"},
        "INVALID": {"posstatus": "open", "pair": "ETHUSD", "type": "sell", "vol": "1",
                    "vol_closed": "0", "cost": "100", "fee": "0", "margin": "50", "time": "NaN"},
    })
    assert [row.txid for row in rows] == ["VALID"]


def test_close_quantity_rejects_non_finite_amounts():
    with pytest.raises(ValueError, match="INVALID_OR_UNPROVEN"):
        resolve_close_quantity(requested=D("NaN"), tracked_pair_quantity=D("1"),
                               free_wallet_quantity=D("1"), product_type="CASH_SPOT")
