# Architektur und implementierter Datenfluss

## Tatsächliche Bausteine

- `app/config.py`: Konfigurationsparsing und source-owned absolute Risikodeckel.
- `app/kraken.py`: Kraken Spot Public-/Private-REST, Signatur, persistente Nonce-Schnittstelle, Mehrdeutigkeit nach Submit-Timeout und History-Endpunkte `TradesHistory`/`Ledgers`.
- `app/history_sync.py`: persistente offsetbasierte, paginierte und idempotente Erst- und Folgesynchronisierung von Trades und Ledger, mit festem Zeitfenster, begrenztem Seitenbudget je Zyklus und Fail-Closed bei ungültigen Ergebnissen.
- `app/market.py`: aktuelle Instrument-Metadaten; nur explizite reguläre `currency`-Basisklassen mit Online-USD-Quote; sichere Ticker-/OHLC-Validierung und konservative Lot-Rundung.
- `app/strategy.py`: deterministische H1-Time-Series-Momentum-/Trend-Hypothese, Stop-Distanz-Proxys, Teilgewinnmengen-Helfer.
- `app/position_management.py`: deterministischer, pro Exchange-Positions-ID arbeitender Shadow-Exit-Planer für Teilgewinn und Stop-Risikoreduktion; keinerlei eigenständige Orderübermittlung.
- `app/backtest.py`: chronologischer OHLCV-Szenario-Simulator mit realistischen Grenzen klar benannt, konservativer voller Margin-Kostenbasis, konto-gewichteter Equitykurve und Drawdown-/Tail-/Kostenkennzahlen.
- `app/costs.py`: Gesamtkostenmodell; wenn tatsächliche Handels-/Margin-Kosten unbekannt sind, Status `INCOMPLETE`.
- `app/risk.py`: risikobasierte Notionalgröße und unabhängiges Entry-Gate.
- `app/execution.py`: Order-Intent, strikte Parametervalidierung, persistente Order-State-Machine und zentrale Order Authority. Es gibt keinen separaten Compile-Time-Release-Schalter; unabhängige Laufzeit-Gates blockieren aktuell reguläre Entries.
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
6. Bestehende einzelne Margin-Positionen werden mit aktuellen Ticker-Seiten und konfigurierter Brutto-Preisbewegungs-Schwelle geprüft; alle Exit-Entscheidungen werden mit Restmenge und Mindestmengenprüfung im Shadow-/Audit-Pfad persistent erfasst. Das ist keine exchange-native Schutzorder und kein Nettogewinnnachweis.
7. Kostenplaner bevorzugt Margin nur bei vollständigen Kosteninputs und positivem Edge-Proxy; ein LONG darf nur mit eigenständig vollständigem Kostenplan auf Cash-Spot zurückfallen. SHORT wird nie auf einen Spot-Sell degradiert. Fehlende Margin-Raten, Reconciliation sowie verifizierte Drawdown-/Tages-PnL-Baselines blockieren jede Risikoerhöhung.
8. Exchange-Order-Snapshots mit Rohantwort persistieren und eindeutige lokale Order-Intents anhand Exchange-Status, Client-ID und ausgeführtem Volumen fail-closed abgleichen; Mehrdeutigkeiten und nicht auflösbare Submitzustände schalten den Circuit Breaker. Das bestätigt nur die Order-State-Zuordnung, nicht das Gesamtportfolio. Steuerexport aus bereits gespeicherten Ereignissen aktualisieren; Quellabdeckung bleibt stets `false`, solange kein unabhängiger kompletter Quellenabgleich nachgewiesen ist. Kandidaten im Shadow-Modus werden atomar als terminale `SHADOW_ONLY`-Intents gespeichert. Ein positiver, aber unkalibrierter Edge-Proxy besteht das unabhängige Risikogate nicht. Die zentrale Order Authority kann API-Parameter validieren, aber die expliziten LIVE-Schalter steuern, ob genehmigte Requests tatsächlich gesendet werden; vollständige Daten-/Risiko-Gates bleiben unabhängig aktiv.

## Sicherheitsinvarianten

- Keine stillen Fallbacks von Margin-Short auf normalen Spot-Verkauf.
- Kein künstliches Aufrunden von Volumen bis zur Mindestorder.
- Freier Walletbestand ist keine Provenienz einer paarbezogenen Strategieposition.
- Nach unklarem Submit-Status kein blinder Wiederholungsauftrag.
- Der Status eines Steuerereignisses oder einer PnL-Schätzung wird nicht automatisch als verifiziert betrachtet.
- Die LIVE-Operator-Schalter ersetzen keine unabhängigen Daten-, Kosten-, Strategie-, Portfolio- und Risiko-Gates; deren Nachweise müssen im Runtimepfad selbst vollständig sein.

## Bekannte Lücken

Die Trade-/Ledger-Erstbefüllung läuft schrittweise über gespeicherte Seiten-Cursor und wiederholt bei Folgeläufen ein überlappendes Zeitfenster. Sie ist noch keine durchgängige Order-/Fill-/OpenPosition-/Ledger-Abstimmung: externe/manuelle Trades, offene/geschlossene Orders, Reservierungen, Kredite, USDC-Konversionen und Crashfenster sind noch nicht verifiziert. Exchange-seitige Schutzorder-Recovery, tatsächliche FX-/USDC-Zuordnung sowie ein realistischer Orderbuch-Backtest und Forward-Shadow-Test fehlen ebenfalls. Ein vollständig importierter Verlauf ist nicht gleichbedeutend mit vollständig reconciliertem Portfolio. Diese Lücken verhindern die Live-Freigabe.