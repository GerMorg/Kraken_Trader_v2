# Legacy-Learnings aus Kraken-Trade-Real

**Regel:** Das Altprojekt ist nur Referenz. Dieses Dokument rechtfertigt keine Übernahme unbesehen kopierten Codes und ändert keine Altprojekt-Datei.

## Historisch geprüfter Befund

Das Audit `kraken_trade_real/docs/AUDIT_2026-10-14.md` untersuchte insbesondere den Stand v0.1.89 sowie Logs vom 13.–14. Oktober 2026. Es beschrieb einen reproduzierbaren Fehlerpfad, in dem ein Spot-Exit die freie Base-Asset-Walletmenge statt der paar-/strategiespezifisch nachgewiesenen Menge als Auftragsmenge nutzte. Im Log betrug die Ordermenge bei ZRC/USD 37,631.29020 gegenüber 36,358.14303 angefordert. Das Audit dokumentierte außerdem übergroße oder nicht zuordenbare Fills, 0 verifizierte Nettotrade-Ergebnisse und fehlende vollständige Ausführungssimulation. Diese Befunde beziehen sich auf den genannten historischen Stand; spätere Änderungen sind separat zu verifizieren.

## Übernommene Lehren

1. **Paar-Lots und Wallet sind verschieden.** `resolve_close_quantity` akzeptiert keine Menge oberhalb belegter Positionsprovenienz. Wallet-Fungibilität über mehrere USD-/andere Quote-Märkte darf kein lokales Strategie-Lot vergrößern.
2. **Fill-ID und Ledger-Ereignis sind Primärdaten.** Echte Fee-Currency, Fill-Preis/-Menge, Margin-Funding und FX-Zeitpunkt müssen aus Börsenantwort und Ledger rekonstruiert werden; geschätzte Werte bleiben geschätzt.
3. **Forecast ist nicht Trade-Ergebnis.** Ein hypothetischer Kursweg oder ein Richtungstreffer beweist weder tatsächliche Ausführbarkeit noch Kosten- und Finanzierungsergebnis.
4. **Confidence ist keine Wahrscheinlichkeit.** Es gibt keine News-/LLM-Signale oder automatische Parameterpromotion in v1.
5. **Order-Timeout ist unbekannt, nicht automatisch fehlgeschlagen.** Status, Client-ID, Trade History, offene Orders/Positionen und Ledger müssen zuerst abgeglichen werden.
6. **Interner Watcher ist kein Börsen-Stop.** Netzwerkausfall, HA-Stillstand, Rate Limits und Gap-Risiko sind eigene Freigabekriterien.
7. **Gebühren dürfen nicht erfunden werden.** Kontospezifische Maker-/Taker-Rate sowie Margin-Eröffnungs- und Rollover-Kosten brauchen Quellen, Zeitpunkt, Währung und Bewertungsbasis.

## Aktuelle Umsetzungszuordnung

| Lehre | Umsetzung im neuen Projekt | Status |
|---|---|---|
| Paarbezogene Ausstiegsmenge | `portfolio.resolve_close_quantity` | Unit-testet; vollständige Exchange-Reconciliation offen |
| Wiederholungs- und Zustandskontrolle | SQLite eindeutige Decision-/Client-IDs, Order-State-Machine | Basis vorhanden; Race-/Restart-Integrationstest offen |
| Nonce nach Neustart | persistente SQLite Nonce | Unit-testet; echte private API noch nicht validiert |
| Kosten-/Funding-Lücken | Kostenmodell gibt bei unbekannten Margin-Raten `INCOMPLETE` zurück | absichtlich Entry-blockierend |
| Forecast vs Ausführung | Signaloutput markiert nicht kalibriert; Backteststatus Szenario-only | OOS-/Forward-Test offen |
| Externe Signale | kein News/Gemini-Modul | entfällt absichtlich |
| Live-Freigabe | Quellcodekonstante auf `False` | gesperrt |

## Nicht übernehmen

Das neue Projekt übernimmt keine Legacy-Lernengine, keinen News-/Gemini-Alpha-Zuschlag, keine Tactical-HFT-ähnliche Nebenstrategie, keine Futures- oder tokenisierte-Aktien-Unterstützung und keine implizite pauschale Margin-Finanzierungsrate.

## Zusätzliche Datenintegritätsprüfungen (16.10.2026)

- Jahres-Steuerreports dürfen nicht nur anhand von Ereignisanzahl und Event-IDs gecacht werden: Reconciliation kann bestehende Ereignisse später mit tatsächlichen Gebühren, FX-Werten oder Verifikationsflags anreichern. Der Runtime-Cache vergleicht daher den vollständigen kanonisierten Ereignisinhalt; ein Regressionstest prüft die Aktualisierung bei gleichbleibender ID und Anzahl.
- Bei einer OHLC-Abfrage für ein einzelnes Instrument darf bei fehlendem erwarteten Pair-Key nur ein eindeutig einzelner Aliasdatensatz als Antwort gelten. Mehrere mögliche Ergebnisreihen werden geschlossen verworfen; ein Regressionstest schützt gegen die Zuordnung fremder Marktpreise zum aktuellen Pair.

Diese Änderungen sind Datenintegritäts-Härtungen, kein Nachweis für vollständige Positions-/Ledger-Reconciliation, Ausführungsqualität oder Profitabilität. Das interne Live-Release-Gate bleibt geschlossen.


## Erweiterte Laufzeit- und Orderintegritätsprüfungen (16.10.2026)

- Öffentliche Kraken-REST-Anfragen werden im Client mindestens eine Sekunde auseinandergehalten; die Drosselung ist mit injizierbarer Uhr getestet. Das ist insbesondere wegen der paarbezogenen Quote-Erneuerung wichtig und darf bei zukünftiger Parallelisierung nicht umgangen werden.
- Quotes werden nach den potenziell langen History-/Account-Aufrufen erneuert und unmittelbar vor einer Kandidatenbewertung paarbezogen abgefragt. Für die Auftragsmengenberechnung gilt Ask für Long und Bid für Short; Last-Price ist keine ausführbare Einstiegsnotierung.
- Ticker mit fehlendem oder nicht positivem 24-Stunden-Quote-Umsatz werden nicht als Kandidaten akzeptiert. Mehrdeutige OHLC-Alias-Antworten werden verworfen.
- Orderstatus-Reconciliation prüft zusätzlich zum Client-ID-Match den Instrument-Alias, die Richtung und die ursprüngliche Sollmenge. Abweichungen bleiben ungelöst und lösen einen persistenten Circuit Breaker aus.
- Eine potenziell angenommene `AddOrder`-Anfrage mit unlesbarer HTTP-/JSON-Antwort, ungültiger Result-Struktur oder fehlender Transaktions-ID bleibt `UNKNOWN_RECONCILING`; sie wird nicht als sicher abgelehnt behandelt und nicht blind erneut gesendet.

Diese Regressionen beweisen die jeweils geprüften Fehlerfälle; sie ersetzen weder einen vollständigen Neustart-/Crash-Recovery-Integrationstest noch den Konto-, Kredit-, USDC-, Transfer- und Gesamtportfolioabgleich. Das Source-Release-Gate bleibt geschlossen.
