from decimal import Decimal
import pytest

from app.portfolio import expected_eea_funding_currency,parse_open_margin_positions,resolve_close_quantity

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
