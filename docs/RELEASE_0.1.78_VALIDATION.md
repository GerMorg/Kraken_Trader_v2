# Version 0.1.78 — Validierung und Release-Grenzen

## Konfigurierbare Verlustgrenzen

- Die HA-Optionen risk_daily_loss_pct und risk_max_drawdown_pct sind nicht mehr durch code-owned Maximalwerte von 3 % bzw. 8 % begrenzt.
- Beide Werte werden unverändert als positive, endliche Dezimalzahlen übernommen. Null, negative, NaN- oder unendliche Werte fallen auf 3 % bzw. 8 % zurück.
- Die Werte sind sichtbarer Teil von PORTFOLIO_ENTRY_RISK_STATUS und werden sowohl in evaluate_entry als auch der unabhängigen Portfolio-Einstiegsprüfung verwendet.
- Die eigenständigen Grenzen für Trade-Risiko, Positionsgröße, Gross-/Net-Exposure, Margin-Auslastung, Mindestreserve, Anzahl offener Positionen und Spot-Hebel bleiben unverändert.

## Futures-Demo-H1-Signalstrecke

- Nutzt die separate Kraken Futures Charts API /api/charts/v1/trade/{symbol}/1h über den fest gepinnten Demo-Host.
- Prüft Symbol, Tick-Typ, Auflösung, Anzahl, OHLC-Werte und Antwortschema.
- Verwendet ausschließlich abgeschlossene H1-Kerzen und verlangt mindestens 73 zeitlich zusammenhängende Stundenwerte. Lücken, doppelte oder ungültige Zeitstempel sowie veraltete Kerzen blockieren die Signalbewertung.
- Ermittelt das Futures-Universum als Schnittmenge aus handelbaren linearen PF_*USD-Perpetuals und den aktuell ausgewählten Spot-USD-Krypto-Basisassets. Tokenisierte Aktien, Rohstoff-/Indexprodukte und Fixed-Maturity-/Inverse-Kontrakte bleiben ausgeschlossen.
- Bewertet höchstens fünf Futures-Märkte pro HA-Zyklus, priorisiert nach USD-Quote-Volumen (nur zur Auswahl, nicht als Kosten- oder Edge-Annahme).
- Protokolliert FUTURES_DEMO_SIGNAL_EVALUATED und FUTURES_DEMO_SIGNAL_SUMMARY. Es werden keine Order-Intents oder Futures-Orders erstellt.

## Was weiterhin die Orderfreigabe blockiert

- Kein nachgewiesener positiver Nettovorteil außerhalb des Trainingszeitraums.
- Handelsgebühren, Spread und Slippage nicht als vollständiger Kostenpfad über Entry und Exit validiert.
- Funding für tatsächliche Haltedauer und Finanzierungszeitpunkte noch nicht vollständig in die Nettokosten integriert.
- Tatsächliche Initial-Margin-/Maintenance-Margin-Tierdaten des Instruments und der Positionsgröße noch nicht abschließend mit dem EEA-Konto abgeglichen.
- Futures-Demo-Orderadapter, Fill-/Positionsreconciliation, Schutzorder-Lifecycle und Neustart-Recovery noch nicht aus der normalen Runtime orchestriert.
- Produktions-Futures bleibt Read-only; Spot/Margin Live-Release-Gate bleibt geschlossen.

## HA-Update

Die HA-Felder risk_daily_loss_pct und risk_max_drawdown_pct können jetzt über 3 bzw. 8 gesetzt werden. Nach dem Update müssen die tatsächlich übernommenen Werte im nächsten PORTFOLIO_ENTRY_RISK_STATUS kontrolliert werden. Bestehende Werte in /data/options.json bleiben erhalten.
