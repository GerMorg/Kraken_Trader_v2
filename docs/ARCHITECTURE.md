# Architektur und implementierter Datenfluss

## Tatsächliche Bausteine

- `run.sh`/Container: Der Entrypoint verwendet `/data/state` und `/data/logs` direkt; er erzeugt dort keine Kindverzeichnisse und führt niemals `chmod` auf `/data`-Pfaden aus. Für die kontrollierte Legacy-Datenbankmigration werden die vorhandenen Mountverzeichnisse während der root-Phase vorübergehend root-owned gemacht, danach werden Verzeichnis- und oberste Dateieigentümer an UID/GID `10001:10001` zurückgegeben, ohne in historische, möglicherweise nicht durchsuchbare Unterverzeichnisse abzusteigen. SQLite liegt unter `/data/state/kraken_trader.sqlite3`, der AppLog unter `/data/logs/events.jsonl`. `/data/options.json` bleibt root-owned und wird als temporäre Datei mit Modus `0400` unter `/run` bereitgestellt. Ein fehlgeschlagener Besitzwechsel oder ein nicht beschreibbarer Pflichtpfad stoppt den Start.
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
- `app/persistence.py`: SQLite/WAL unter `/data/state/kraken_trader.sqlite3`, Bar-/Ereignis-/Intent-/Fill-/Ledger-/Steuerhistorie und persistente Circuit Breaker. Das Container-Entrypoint migriert den älteren Datenbankpfad samt SQLite-Seitendateien vor dem UID-Wechsel, erhält Root-Eigentum des Mount-Parents und bricht bei widersprüchlichen Datenbankzuständen ab.
- `app/audit.py`: vollständige redigierte Ereignisdatensätze in der rotierenden JSONL-Datei `/data/logs/events.jsonl`; eigenständige kompakte JSON-Console-Ansicht in stdout. Kompakte Felder und Report-Deduplizierung betreffen ausschließlich stdout. Die Datei behält alle exakten Werte und Wiederholungen.
- `app/backtest.py`: eingeschränkter OHLCV-Szenario-Simulator.
- `app/strategy_lab.py`: isoliertes Research-Modul für streng validierten Kraken-Historienimport und den Vergleich von TSM, Mean Reversion und logistischer Regression. Die historischen CLI-Analysen bleiben offline; die kleine Signalberechnung für parallele Kandidaten wird im Runtime-Zyklus nur als SHADOW-Messung aufgerufen.
- `strategy_candidate_observations` in SQLite: eigenständige, idempotente Research-Tabelle mit allen Kandidaten-Signalen einschließlich NONE, Modell-/Feature-Metadaten und späteren Forward-Labels. Sie ist getrennt von `intents`, `fills` und der bisherigen TSM-Research-Tabelle.
- `app/tax.py`: Jahres-Export JSON/CSV/Markdown mit Datenlücken- und manuellen Prüfhinweisen.
- `app/runtime.py`, `app/main.py`: Zyklussteuerung, Startdiagnose und kontrolliertes Stoppen.

## Research-Pfade ab Version 0.1.47/0.1.48

`scripts/compare_strategies.py` liest eine lokal bereitgestellte Kraken-OHLCVT-CSV, berechnet deren SHA-256 und ruft `app.strategy_lab` auf. Der Bericht enthält Quellmetadaten, erkannte Datenlücken, die festen Train-/Validation-/Test-Grenzen, die nur auf Train gefitteten ML-Koeffizienten, kostenbereinigte Forward-Ergebnisse und Margin-Kostensensitivitäten. Weder importierte Kerzen noch Modellparameter werden in die Live-Persistenz geschrieben. Die einzige Ausgabe ist stdout und optional eine JSON-Datei des explizit angegebenen Pfads.

Der erste Schritt ist ein fester chronologischer Holdout, kein vollständiger Portfolio- oder Orderbuchsimulator und keine expanding-window Walk-Forward-Schätzung. Der Report ist immer als Research-only markiert und kann kein Laufzeit-Gate ändern.

Ab Version 0.1.48 erzeugt der laufende SHADOW-Zyklus zusätzlich drei idempotente Parallelbeobachtungen auf jeder frischen H1-Kerze. Das Walk-Forward-ML trainiert nur auf Zielkerzen, die vor dem Entscheidungsbalken liegen. Kandidatensignale werden nicht in die Entry-Logik übertragen; das bestehende TSM-Research bleibt davon getrennt. Die neue Zusammenfassung lautet `SHADOW_STRATEGY_LAB_SUMMARY` und kann keine Order oder Release-Freigabe auslösen.

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