import json

from app.tax import AustrianTaxReports

def test_tax_reports_mark_missing_fx_and_manual_review(tmp_path):
    result=AustrianTaxReports(str(tmp_path)).generate_year(2026,[{
      "event_id":"E1","ts":"2026-01-01T00:00:00Z","year":2026,
      "event_type":"MARGIN_SETTLEMENT","asset":"BTC","value_eur":None,
      "verified":False,"manual_review":True}])
    report=json.loads((tmp_path/"2026"/"income-tax-report.json").read_text())
    assert result["status"]=="INCOMPLETE_REQUIRES_MANUAL_REVIEW"
    assert report["missing_eur_valuation_event_ids"]==["E1"]
    assert (tmp_path/"2026"/"tax-events.csv").is_file()
    assert (tmp_path/"2026"/"income-tax-information.md").is_file()

def test_report_refuses_mixed_tax_year(tmp_path):
    import pytest
    with pytest.raises(ValueError,match="YEAR_MISMATCH"):
        AustrianTaxReports(str(tmp_path)).generate_year(2026,[{"event_id":"E2","year":2025}])


def test_empty_or_unreconciled_source_cannot_be_reported_as_complete(tmp_path):
    report = AustrianTaxReports(str(tmp_path)).generate_year(2026, [])
    assert report["status"] == "INCOMPLETE_REQUIRES_MANUAL_REVIEW"
    assert report["source_coverage_complete"] is False
    assert (tmp_path / "2026" / "income-tax-report.json").exists()
    assert (tmp_path / "2026" / "tax-events.csv").exists()
    assert (tmp_path / "2026" / "income-tax-information.md").exists()
