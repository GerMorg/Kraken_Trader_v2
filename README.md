# Kraken Trader v2

Eigenständige Home-Assistant-App für Kraken Spot und Spot Margin. Dieses Repository startet bewusst als neue, schlanke Implementierung; es kopiert die alte App nicht. Die verbindliche Spezifikation steht in [Anweisung.txt](Anweisung.txt).

## Aktueller Status — 0.1.21

**Version 0.1.21 sendet `GetApiKeyInfo` entsprechend der aktuellen Kraken-Dokumentation mit `Content-Type: application/json` und signiert den exakten JSON-Body. Die übrigen privaten REST-Endpunkte behalten ihren bestehenden Form-Transport. Die Schutzmaßnahmen aus 0.1.20 – serialisierte Nonces, Boolesche Formwerte und Fallback bei fehlenden Metadaten – bleiben erhalten. Sicherer SHADOW-/Datenprüfungsmodus; nicht für Live-Handel freigegeben.** Es gibt keinen zusätzlichen Compile-Time-Freigabeschalter: `mode: LIVE`, `live_enabled: true` und `kill_switch: false` erlauben der zentralen Order Authority nur dann eine Übermittlung, wenn sämtliche Laufzeit-Gates bestanden sind. In dieser Version verhindern der unkalibrierte Strategie-Edge, unbekannte tägliche PnL-/Drawdown-Baselines, die noch unvollständige Gesamtportfolio-Reconciliation und unbestätigte Margin-Kosten weiterhin neue Risikoerhöhungen. Das bloße Umschalten der HA-Optionen wird daher derzeit keine reguläre Entry-Order freigeben. Die Installation dient zuerst dem sicheren Import und der Prüfung realer Account-/History-Daten; das ist keine Profitabilitäts- oder Live-Sicherheitsfreigabe.

## Installation in Home Assistant OS

1. In Home Assistant unter **Einstellungen → Apps** das Add-on-Repository `https://github.com/GerMorg/Kraken_Trader_v2` hinzufügen.
2. `Kraken Margin Trader` installieren und Konfiguration speichern.
3. Zunächst `mode: SHADOW`, `live_enabled: false` und `kill_switch: true` beibehalten.
4. Zugangsdaten erst nach eigener Prüfung als API-Key mit minimalen benötigten Rechten eintragen. Keine Auszahlungsberechtigung vergeben.
5. App starten und das App-Protokoll ansehen. Dauerhafte Ereignisse liegen unter `/data/logs/events.jsonl`; Datenbank unter `/data/kraken_trader.sqlite3`. Die Steuerexporte sind unter `/config/reports/tax/AT/<Jahr>/` über den persistenten App-Konfigurationsordner zugänglich. Zunächst keine Handelsrechte vergeben und keine LIVE-Schalter aktivieren.

Die Anwendung verwendet keine HA-Sensoren und kein Dashboard. Alle Zeitpunkte im Log sind UTC.

## Daten- und Ausführungsintegrität (v0.1.21)

Neue Risikoerhöhungen werden zusätzlich ausgesetzt, solange Trades-, Ledger- oder Closed-Orders-Historie fehlt, ein Backfill unvollständig ist oder ein Quellfehler vorliegt. Die bestehende Positionsüberwachung läuft weiter. Vor dem Backfill prüft `GetApiKeyInfo` die erforderlichen privaten Leserechte, Ablaufzeit und History-Zeitfenster, ohne API-Key-Werte zu protokollieren; bekannte Konfigurationsfehler werden zyklisch neu bewertet. Die öffentliche REST-API wird gedrosselt; frische Ask-/Bid-Quotes, valide Marktumsätze und unambiguous OHLC-Daten sind Pflicht für neue Kandidaten. Exchange-Order-Reconciliation prüft Richtung, Instrument und Sollmenge; unbekannte AddOrder-Antworten bleiben ungelöst statt als Ablehnung markiert zu werden. Diese Prüfungen ersetzen nicht die dokumentierten offenen Voll-Reconciliation- und Forward-Test-Gates.

## Position Management (seit v0.1.5)

Der Shadow-Pfad wertet vorhandene, einzelne Kraken-Margin-Positionen jetzt pro Positions-ID aus. Standardmäßig wird bei einer **Brutto-Kursbewegung** von +3,5 % ein Teilverkauf von 50 % der **verbleibenden offenen Menge** vorgeschlagen; bei −3,0 % wird eine Risikoreduktion über die gesamte verbleibende Menge vorgeschlagen. Der Ausstiegskurs verwendet die relevante Ticker-Seite (Bid für Long-Exits, Ask für Short-Exits); Lotpräzision und Mindestmenge werden ohne Aufrunden eingehalten.

Das sind zunächst protokollierte Shadow-Vorschläge, **keine** automatisch gesendeten Orders. Gebühren, Margin-Open-/Rollover-Finanzierung, Slippage jenseits der verwendeten Bid/Ask-Seite und Steuer-PnL sind nicht Teil dieser einfachen Kursbewegungs-Schwelle. Es gibt keine bestätigte exchange-native Stop-Order; bei HA-/Netzausfall wird daher kein Schutz durch diesen lokalen Plan garantiert. Der Runtimepfad dieser Vorabversion sendet keine regulären Entries, weil Strategie-Edge, tägliche PnL-/Drawdown-Baselines, vollständige Gesamtportfolio-Reconciliation und echte Margin-Kostenevidenz noch nicht als erfüllt nachgewiesen sind. Die lokalen Teilgewinn-/Stop-Schwellen bleiben Empfehlungen, keine börsenseitigen Schutzorders.

## Produktumfang

- Dynamische Erkennung von Online-Krypto-Spot-Märkten mit USD-Quote; tokenisierte Aktien und andere Quote-Währungen werden ausgeschlossen.
- Einfache, reproduzierbare H1-Time-Series-Momentum-/Trendfolge-Hypothese mit getrennten Long-/Short-Signalen.
- Risikoprüfung und Positionsgrößenberechnung auf `Decimal`.
- Kraken REST-Authentifizierung, eindeutige Client-Order-IDs, persistente Order-Intents, SQLite-Ereignisse und JSONL-Protokollierung.
- Persistenter, paginierter und idempotenter Import von Kraken Trades History und Ledgers mit Fail-Closed-Verhalten bei unvollständigen Seiten.
- Separate Datenmodelle für Margin-Positionen/USDC-Finanzierung sowie automatisch aktualisierte Jahresberichte für alle importierten Steuerjahre; fehlende FX-/Quellabdeckung bleibt explizit markiert.

## Wichtige Einschränkungen

Die vorhandenen Signale sind Hypothesen, keine kalibrierten Wahrscheinlichkeiten. Kosten und geschätzte Marktrendite sind nicht als bewiesener Nettovorteil zu verstehen; eine positive unkalibrierte Edge-Schätzung kann die Risikofreigabe nicht passieren. Shadow-Kandidaten bleiben getrennt von ausführbaren Order-Intents. Unbekannte Gebühren, unvollständige Account-Snapshots oder ungeklärte Reconciliation müssen Trades blockieren. Der L2-Replay benötigt getrennte, nach Quellen-ID, Zeitstempel, Quote-Währung und Evidenzqualität dokumentierte Margin-Eröffnungs- und Rolloverkosten; ein teilweise gefüllter Einstieg ohne modellierte Stornierung des Restauftrags bleibt ungelöst. Zusätzlich zum OHLCV-Szenario-Modell existiert ein zeitgestempelter L2-Orderbuch-Replay für sichtbare Tiefe, VWAP, Teilfüllungen, Gebühren und explizite Margin-Finanzierung. Er ist weiter ein deterministisches Szenario-Modell: Es liegen keine validierten historischen L2-Daten vor, und Queue-Priorität, Börsenlatenz, Cancel-/Reject-Semantik sowie konto-/instrumentbezogene Funding-Ledger sind nicht vollständig abgebildet. Deshalb gibt es noch keinen belastbaren Out-of-Sample- oder Forward-Shadow-Nachweis. CI-Grün allein belegt keine Profitabilität und keine Sicherheit bei Ausfall.

Siehe [docs/OPERATIONS.md](docs/OPERATIONS.md), [docs/MARGIN_OPERATIONS.md](docs/MARGIN_OPERATIONS.md) und [docs/EXECUTION_AND_RECOVERY.md](docs/EXECUTION_AND_RECOVERY.md).
