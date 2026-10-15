# Betrieb auf Home Assistant OS

## Repository/App Installation

1. In Home Assistant **Einstellungen → Apps → App-Repository hinzufügen** wählen.
2. `https://github.com/GerMorg/Kraken_Trader_v2` als benutzerdefiniertes Repository hinzufügen.
3. `Kraken Margin Trader` installieren, Add-on starten und das App-Protokoll prüfen.
4. `mode: SHADOW`, `live_enabled: false` und `kill_switch: true` als Ausgangsmodus belassen.

## API-Key-Rechte

Keine Auszahlungsrechte. Für das Lesen benötigt der Schlüssel die in der aktuellen Kraken-Dokumentation aufgeführten Rechte für Kontostand/Trade Balance und offene/geschlossene Orders, Trades und Positionen. Für spätere Orders wären Handelsrechte nötig; kein solcher Orderbetrieb ist derzeit freigegeben. Schlüssel nur über das Home-Assistant-App-Konfigurationsfeld eintragen und nie in Issues oder Logs kopieren.

## Einstellungen

- `mode`: `SHADOW` oder `PAPER` sind die sicheren Modi; `LIVE`-Option ist derzeit durch das interne Source-Gate wirkungslos für echte Ausführung.
- `live_enabled`: zusätzlicher UI-Schalter, hebt keine internen Grenzen auf.
- `kill_switch`: sicherer Default `true`.
- Marktscanintervall, Datenalter, Kandidatenzahl und TSM-Schwellen können im begrenzten Bereich gesteuert werden.
- Risikogrenzen können verschärft, aber nicht oberhalb der Source-Caps gesetzt werden.
- Keine Gemini-/News-API, keine HA-Sensoren und kein Dashboard vorgesehen.

## Logs und Datenspeicher

Home Assistant App Log ist die primäre Bedienoberfläche. Strukturierte UTC-JSON-Zeilen werden nach `/data/logs/events.jsonl` rotiert. SQLite liegt unter `/data/kraken_trader.sqlite3`. Steuerdaten liegen unter `/data/reports/tax/AT/<Jahr>/`.

Achte besonders auf `CYCLE_BLOCKED`, `NO_TRADE`, `RISK_GATE_REJECTED`, `CYCLE_FAILED_CIRCUIT_BREAKER`, `ORDER_OUTCOME_UNKNOWN_RECONCILING` und `LIVE_ORDER_BLOCKED_BY_RELEASE_GATE`. API-Key, Secret, Signatur oder Auth-Header gehören nicht in die Logs.

## Aktueller Freigabestatus

**Keine Live-Freigabe.** Der Source-Release-Schalter ist `False`, Konto-/Margin-Ledger-Reconciliation fehlt und dynamische Margin-Raten werden nicht ausreichend bestätigt. Erst nach grüner CI, vollständigen Integrations-/Recoverytests, realistischer historischen Prüfung und einem abgeschlossenen Forward-Shadow-Test ist eine neue explizite, geprüfte Freigabe zulässig. Die erste spätere Live-Stufe muss klein, beaufsichtigt und mit getesteter Schutz-/Recoverystrategie erfolgen.