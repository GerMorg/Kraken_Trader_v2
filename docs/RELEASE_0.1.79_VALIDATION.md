# Version 0.1.79 — Validierung und Release-Grenzen

## Risikoparameter

Ohne von der App gesetzte Obergrenzen einstellbar sind:

- Risiko pro Trade, Positionsgröße, Gross-Exposure, Net-Exposure und Margin-Budget.
- Maximaler Spot-Margin-Hebel und maximale Anzahl offener Positionen.
- Tagesverlust- und Drawdown-Limits.
- Strategie-Trendschwellen, Effizienzfilter, Stopdistanz und Haltehorizont.

Die Prozent-Risikowerte müssen endlich und nicht negativ sein. Negative, NaN- oder unendliche Eingaben fallen auf den jeweiligen Standardwert zurück. Der Tagesverlust und Drawdown müssen weiterhin größer als null sein.

`risk_max_open_positions=0` blockiert neue Einstiege. `risk_max_leverage` ist die vom Betreiber gewünschte Obergrenze; ausgewählt wird ausschließlich ein Hebel, den Kraken für das jeweilige Instrument und die jeweilige Orderseite meldet. Es gibt keine eigene Obergrenze wie früher 3x oder 100x in unserer App.

Die Cash-Reserve ist auf ihren mathematisch sinnvollen Bereich von 0 bis 100 % beschränkt. `risk_min_margin_level_pct` ersetzt die bisher fest codierte 300-%-Schwelle; Standard bleibt 300 %. Ein unbekannter Margin-Level-Wert blockiert Hebel-Einstiege weiterhin, auch wenn die numerische Schwelle auf 0 gesetzt wird.

Die Trend-Effizienzschwellen liegen im mathematischen Bereich 0 bis 1. Mindest- und Maximal-Stopdistanz müssen positiv und korrekt geordnet sein. Bei ungültigen Werten werden die Standards verwendet.

## Operative Parameter

- Zyklusintervall: mindestens 60 Sekunden, keine App-Obergrenze. Das Mindestintervall reduziert unnötige API-Aufrufe.
- Kandidatenzahl: mindestens ein Markt, keine App-Obergrenze. Größere Werte erhöhen Kraken-API-Last und Laufzeit.
- Marktdatenalter: mindestens 15 Sekunden, keine App-Obergrenze. Ein zu hoher Wert kann veraltete Signale zulassen; Frischeprüfung bleibt aktiv.

## Futures ohne Demo

- Keine Futures-Demo-Zugangsdaten oder Demo-Snapshots mehr im normalen Runtime-Pfad.
- Futures-Instrumente und Ticker stammen aus dem separaten Produktions-Futures-Read-only-Snapshot, wenn `futures_readonly_enabled` aktiviert und ein verifizierter READ_ONLY-Key vorhanden ist.
- Futures H1-Kerzen werden über `GET https://futures.kraken.com/api/charts/v1/trade/{symbol}/1h` abgerufen.
- Charts-GETs verwenden keine Authentifizierung und keine Mutations-Endpunkte.
- Signalereignisse heißen `FUTURES_MARKET_SIGNAL_*`. Datenqualität und Sperrgründe bleiben erhalten.
- Es entstehen keine Futures-Order-Intents oder Futures-Orders.

## Weiterhin nicht freigegeben

- Futures-Orderausführung, Position-/Fill-Reconciliation, Börsenschutzorders und Neustart-Recovery.
- Nachweis eines positiven Nettovorteils außerhalb des Trainingszeitraums.
- Vollständige historische Gebühren-, Spread-, Slippage-, Funding- und Initial-/Maintenance-Margin-Kalibrierung.
- Spot/Margin Live-Ausführung bleibt durch `INTERNAL_LIVE_EXECUTION_RELEASED = False` gesperrt.

## HA-Update

Nach dem Update die neuen Werte in den HA-App-Optionen kontrollieren. Die folgenden Werte können ohne App-Obergrenze gesetzt werden: `risk_per_trade_pct`, `risk_max_position_pct`, `risk_max_gross_pct`, `risk_max_net_pct`, `risk_max_margin_pct`, `risk_max_leverage`, `risk_max_open_positions`, `risk_daily_loss_pct`, `risk_max_drawdown_pct`, `risk_min_margin_level_pct`, `strategy_fast_bps`, `strategy_slow_bps`, `strategy_fast_efficiency_min`, `strategy_slow_efficiency_min`, `strategy_stop_min_pct`, `strategy_stop_max_pct` und `expected_holding_hours`.

Die Cash-Reserve bleibt auf 0 bis 100 % begrenzt. Bestehende Optionswerte werden nicht automatisch überschrieben; die im AppLog ausgegebenen übernommenen Einstellungswerte sind nach dem Update zu kontrollieren.
