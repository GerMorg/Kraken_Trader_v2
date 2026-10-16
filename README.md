# Kraken Trader v2

Eigenständige Home-Assistant-App für Kraken Spot und Spot Margin. Dieses Repository startet bewusst als neue, schlanke Implementierung; es kopiert die alte App nicht. Die verbindliche Spezifikation steht in [Anweisung.txt](Anweisung.txt).

## Aktueller Status — 0.1.3

**Nur Shadow-/Research-Grundlage. Kein unbeaufsichtigter Live-Handel.** Die Ausführungsfreigabe ist zusätzlich zu den UI-Konfigurationsschaltern in Python fest auf `False` gesetzt. Das ist beabsichtigt: Kontenabgleich, tatsächliche Margin-Gebühren-/Fundingzuordnung, robuste Recovery, historischer Out-of-Sample-Backtest und Forward-Shadow-Test sind noch nicht vollständig nachgewiesen.

## Installation in Home Assistant OS

1. In Home Assistant unter **Einstellungen → Apps** das Add-on-Repository `https://github.com/GerMorg/Kraken_Trader_v2` hinzufügen.
2. `Kraken Margin Trader` installieren und Konfiguration speichern.
3. Zunächst `mode: SHADOW`, `live_enabled: false` und `kill_switch: true` beibehalten.
4. Zugangsdaten erst nach eigener Prüfung als API-Key mit minimalen benötigten Rechten eintragen. Keine Auszahlungsberechtigung vergeben.
5. App starten und das App-Protokoll ansehen. Dauerhafte Ereignisse liegen unter `/data/logs/events.jsonl`; Datenbank unter `/data/kraken_trader.sqlite3`.

Die Anwendung verwendet keine HA-Sensoren und kein Dashboard. Alle Zeitpunkte im Log sind UTC.

## Produktumfang

- Dynamische Erkennung von Online-Krypto-Spot-Märkten mit USD-Quote; tokenisierte Aktien und andere Quote-Währungen werden ausgeschlossen.
- Einfache, reproduzierbare H1-Time-Series-Momentum-/Trendfolge-Hypothese mit getrennten Long-/Short-Signalen.
- Risikoprüfung und Positionsgrößenberechnung auf `Decimal`.
- Kraken REST-Authentifizierung, eindeutige Client-Order-IDs, persistente Order-Intents, SQLite-Ereignisse und JSONL-Protokollierung.
- Persistenter, paginierter und idempotenter Import von Kraken Trades History und Ledgers mit Fail-Closed-Verhalten bei unvollständigen Seiten.
- Separate Datenmodelle für Margin-Positionen/USDC-Finanzierung sowie automatisch aktualisierte Jahresberichte für alle importierten Steuerjahre; fehlende FX-/Quellabdeckung bleibt explizit markiert.

## Wichtige Einschränkungen

Die vorhandenen Signale sind Hypothesen, keine kalibrierten Wahrscheinlichkeiten. Kosten und geschätzte Marktrendite sind nicht als bewiesener Nettovorteil zu verstehen. Unbekannte Gebühren, unvollständige Account-Snapshots oder ungeklärte Reconciliation müssen Trades blockieren. Der erste Backtest-Prototyp verwendet OHLCV-Proxies und ist **kein vollständiger Orderbuch-/Exchange-Ausführungssimulator**. CI-Grün allein belegt keine Profitabilität und keine Sicherheit bei Ausfall.

Siehe [docs/OPERATIONS.md](docs/OPERATIONS.md), [docs/MARGIN_OPERATIONS.md](docs/MARGIN_OPERATIONS.md) und [docs/EXECUTION_AND_RECOVERY.md](docs/EXECUTION_AND_RECOVERY.md).