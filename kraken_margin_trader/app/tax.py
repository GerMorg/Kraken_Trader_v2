from __future__ import annotations

import csv
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any, Iterable

REPORT_FIELDS = ["event_id","ts","year","event_type","asset","quote_asset","quantity",
                 "value_eur","fee_amount","fee_asset","source_ref","verified","manual_review"]

class AustrianTaxReports:
    """Exports traceable evidence for Austrian tax preparation; not tax advice."""

    def __init__(self, root: str = "/data/reports/tax/AT") -> None:
        self.root = Path(root)

    def generate_year(self, year: int, events: Iterable[dict[str,Any]]) -> dict[str,Any]:
        target = self.root / str(year)
        target.mkdir(parents=True,exist_ok=True)
        items=[dict(event) for event in events]
        for item in items:
            if int(item.get("year",year)) != year:
                raise ValueError("TAX_EVENT_YEAR_MISMATCH")
        missing_fx=[str(e.get("event_id","unknown")) for e in items
                    if e.get("value_eur") in (None,"") and e.get("event_type") not in
                    {"LOAN","REPAYMENT","TRANSFER","SETTLEMENT"}]
        unverified=[str(e.get("event_id","unknown")) for e in items if not bool(e.get("verified",False))]
        manual=[str(e.get("event_id","unknown")) for e in items if bool(e.get("manual_review",True))]
        report={"report_type":"AUSTRIA_CRYPTO_INCOME_TAX_SUPPORT","year":year,
                "generated_at":datetime.now(timezone.utc).isoformat(),
                "status":"INCOMPLETE_REQUIRES_MANUAL_REVIEW" if missing_fx or unverified or manual else "EVIDENCE_COMPLETE_REVIEW_REQUIRED",
                "tax_advice":False,"event_count":len(items),
                "missing_eur_valuation_event_ids":missing_fx,"unverified_event_ids":unverified,
                "manual_review_event_ids":manual,"events":items,
                "limitations":["Not official BMF Steuerreporting and not tax advice.",
                 "Missing FX rates, acquisition costs and funding details are never invented.",
                 "Margin open/close/settlement events require product-specific Austrian tax review."]}
        (target/"income-tax-report.json").write_text(json.dumps(report,ensure_ascii=False,indent=2,default=str)+"\n",encoding="utf-8")
        with (target/"tax-events.csv").open("w",encoding="utf-8",newline="") as stream:
            writer=csv.DictWriter(stream,fieldnames=REPORT_FIELDS,extrasaction="ignore")
            writer.writeheader()
            for event in items:
                writer.writerow({field:event.get(field,"") for field in REPORT_FIELDS})
        notes=[f"# Österreichische Einkommensteuer-Unterlagen {year}","",
               f"Status: {report['status']}",f"Erfasste Ereignisse: {len(items)}",
               f"Fehlende EUR-Bewertungen: {len(missing_fx)}",f"Nicht verifiziert: {len(unverified)}",
               f"Manuell zu prüfen: {len(manual)}","",
               "Diese Unterlagen helfen bei der Nachvollziehbarkeit, sind keine Steuerberatung und kein amtliches BMF-Steuerreporting. Unklare Margin-, Settlement-, Finanzierungs- und FX-Ereignisse bleiben manuell zu prüfen.",
               "Offizielle Quelle: https://www.bmf.gv.at/themen/steuern/sparen-veranlagen/steuerliche-behandlung-von-kryptowaehrungen.html",
               "Steuerreporting: https://www.bmf.gv.at/themen/steuern/sparen-veranlagen/steuerreporting.html"]
        (target/"income-tax-information.md").write_text("\n".join(notes)+"\n",encoding="utf-8")
        return {"year":year,"directory":str(target),"event_count":len(items),"status":report["status"],
                "missing_fx":len(missing_fx),"unverified":len(unverified),"manual_review":len(manual)}
