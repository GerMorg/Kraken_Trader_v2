# Architektur und implementierter Datenfluss

## Tatsächliche Bausteine

- `app/config.py`: Konfigurationsparsing und source-owned absolute Risikodeckel.
- `app/kraken.py`: Kraken Spot Public-/Private-REST, Signatur, persistente Nonce-Schnittstelle, Mehrdeutigkeit nach Submit-Timeout und Client-Wrapper für `TradesHistory`/`Ledgers` (noch nicht laufend synchronisiert).
- `app/market.py`: aktuelle Instrument-Metadaten; nur explizite reguläre `currency`-Basisklassen mit Online-USD-Quote; sichere Ticker-/OHLC-Validierung und konservative Lot-Rundung.
- `app/strategy.py`: deterministische H1-Time-Series-Momentum-/Trend-Hypothese, Stop-Distanz-Proxys, Teilgewinnmengen-Helfer.
- `app/costs.py`: Gesamtkostenmodell; wenn tatsächliche Handels-/Margin-Kosten unbekannt sind, Status `INCOMPLETE`.
- `app/risk.py`: risikobasierte Notionalgröße und unabhängiges Entry-Gate.
- `app/execution.py`: Order-Intent, Parametermapping, zentrale Order Authority und intern verriegelte Live-Freigabe.
- `app/portfolio.py`: richtungsgetrennte Brutto-/Nettoexposure-Aggregation, streng geprüfte Margin-Lots, paarbezogene Mengensicherheit und Kennzeichnung erwarteter (nicht kontobestätigter) EWR-Finanzierungswährung.
- `app/persistence.py`: SQLite/WAL, Bar-/Ereignis-/Intent-/Fill-/Ledger-/Steuerhistorie und persistente Circuit Breaker.
- `app/audit.py`: JSONL in stdout und rotierender Datei mit Secret-Redaktion.
- `app/backtest.py`: eingeschränkter OHLCV-Szenario-Simulator.
- `app/tax.py`: Jahres-Export JSON/CSV/Markdown mit Datenlücken- und manuellen Prüfhinweisen.
- `app/runtime.py`, `app/main.py`: Zyklussteuerung, Startdiagnose und kontrolliertes Stoppen.

## Kontrollfluss

1. Systemstatus abfragen; bei Nichtverfügbarkeit Circuit Breaker auslösen.
2. Aktuelle AssetPairs laden und auf online, Krypto, USD-Quote, Minima und Präzision filtern.
3. USD-Märkte über 24h-Quotevolumen begrenzt auswählen; Ticker-Spread prüfen.
4. Geschlossene H1-OHLC-Kerzen speichern; Trendhypothese protokollieren.
5. Aktuelle Konto-, TradeBalance-, OpenOrders- und OpenPositions-Daten abfragen, falls Schlüssel konfiguriert sind.
6. Kostenplaner bevorzugt Margin nur bei vollständigen Kosteninputs und positivem Edge-Proxy; ein LONG darf nur mit eigenständig vollständigem Kostenplan auf Cash-Spot zurückfallen. SHORT wird nie auf einen Spot-Sell degradiert. Fehlende Margin-Raten, Reconciliation sowie verifizierte Drawdown-/Tages-PnL-Baselines blockieren jede Risikoerhöhung.
7. Steuerexport aus bereits gespeicherten Ereignissen aktualisieren; Quellabdeckung bleibt stets `false`, solange kein unabhängiger kompletter Quellenabgleich nachgewiesen ist. Shadow-Intent persistent speichern. Die zentrale Order Authority kann API-Parameter validieren, aber `LIVE_EXECUTION_RELEASED=False` blockiert echte Submit-Requests.

## Sicherheitsinvarianten

- Keine stillen Fallbacks von Margin-Short auf normalen Spot-Verkauf.
- Kein künstliches Aufrunden von Volumen bis zur Mindestorder.
- Freier Walletbestand ist keine Provenienz einer paarbezogenen Strategieposition.
- Nach unklarem Submit-Status kein blinder Wiederholungsauftrag.
- Der Status eines Steuerereignisses oder einer PnL-Schätzung wird nicht automatisch als verifiziert betrachtet.
- Kein Schalter in der Home-Assistant-Konfiguration kann das interne Live-Release-Gate überschreiben.

## Bekannte Lücken

Noch nicht implementiert/nachgewiesen sind eine durchgängige Order-/Fill-/OpenPosition-/Ledger-Abstimmung einschließlich externer Trades, Reservierung über alle Quote-Paare und Crashfenster, exchange-seitige Schutzorder-Recovery, echtes FX-/USDC-Ledger-Mapping, sowie ein realistischer orderbuchbasierter Backtest und Forward-Shadow-Test. Diese Lücken verhindern die Live-Freigabe.