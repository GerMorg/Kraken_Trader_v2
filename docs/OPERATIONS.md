# Betrieb auf Home Assistant OS

## Repository/App Installation

1. In Home Assistant **Einstellungen → Apps → App-Repository hinzufügen** wählen.
2. `https://github.com/GerMorg/Kraken_Trader_v2` als benutzerdefiniertes Repository hinzufügen.
3. `Kraken Margin Trader` installieren, Add-on starten und das App-Protokoll prüfen.
4. `mode: SHADOW`, `live_enabled: false` und `kill_switch: true` als Ausgangsmodus belassen.

## API-Key-Rechte

Keine Auszahlungsrechte. Für das Lesen benötigt der Schlüssel die in der aktuellen Kraken-Dokumentation aufgeführten Rechte für Kontostand/Trade Balance und offene/geschlossene Orders, Trades und Positionen. Für spätere Orders wären Handelsrechte nötig; kein solcher Orderbetrieb ist derzeit freigegeben. Schlüssel nur über das Home-Assistant-App-Konfigurationsfeld eintragen und nie in Issues oder Logs kopieren.

Vor dem privaten Account-Backfill ruft die App `GetApiKeyInfo` auf und prüft Query-Rechte, Ablaufzeit und den erlaubten historischen Abfragezeitraum. Die Prüfung gibt ausschließlich eine bereinigte Allowlist aus; das von Kraken mitgelieferte Feld `apiKey` wird nicht in Audit-Ereignisse übernommen. Ein fehlendes Leserecht, ein abgelaufener/unklarer Schlüssel oder ein eingeschränkter Zeitraum blockiert den sicheren Account-Read-Pfad. Der Block wird je Zyklus erneut geprüft und nicht als unumkehrbarer Konfigurations-Circuit-Breaker gespeichert. Hat der Schlüssel zwar alle Leserechte, aber eine unerlaubte Auszahlungsberechtigung oder fehlen Rechte für eine angeforderte reine Kraken-Validierung, werden neue Entries blockiert; vorhandene Positionen werden nach Möglichkeit weiter überwacht.

## Einstellungen

- `mode`: `SHADOW` oder `PAPER` sind die sicheren Modi; `LIVE`-Option ist derzeit durch das interne Source-Gate wirkungslos für echte Ausführung.
- `live_enabled`: zusätzlicher UI-Schalter, hebt keine internen Grenzen auf.
- `kill_switch`: sicherer Default `true`.
- Marktscanintervall, Datenalter, Kandidatenzahl und TSM-Schwellen können im begrenzten Bereich gesteuert werden.
- Risikogrenzen können verschärft, aber nicht oberhalb der Source-Caps gesetzt werden.
- Keine Gemini-/News-API, keine HA-Sensoren und kein Dashboard vorgesehen.

## Logs und Datenspeicher

Home Assistant App Log ist die primäre Bedienoberfläche. Strukturierte UTC-JSON-Zeilen werden nach `/data/logs/events.jsonl` rotiert. App- und Strategieversion werden zentral aus Paketmetadaten übernommen. SQLite liegt unter `/data/kraken_trader.sqlite3`. Öffentliche Kraken-Spot-REST-Aufrufe werden clientseitig mit mindestens einer Sekunde Mindestabstand gedrosselt. Der Runtimezyklus erneuert nach History-/Kontozugriffen die Quotes und fragt einen Kandidaten-Ticker vor der jeweiligen Bewertung nochmals ab; ein vollständiger Scan kann dadurch mehrere Sekunden bis Minuten benötigen. Mit konfigurierten API-Zugangsdaten importiert der Runtimezyklus Trades History und ClosedOrders mit persistenten Kraken-Cursor-Tokens und Ledgers offsetbasiert in begrenzten Seitenbudgets; das Tax-Reporting aktualisiert danach die Belegdateien unter `/data/reports/tax/AT/<Jahr>/`. Offene Einzelpositionen werden mit historisierten Trade-/Order-/Ledger-Referenzen geprüft, aber Wallet-Balanceänderungen zwischen zwei Account-Snapshots werden gegen einen nach dem Snapshot erneuerten Kraken-Ledger-Zeitabschnitt geprüft; bei unerklärter Änderung wird ein Circuit Breaker ausgelöst. Das ist nur ein inkrementeller Wallet-Abgleich, kein abgeschlossener Währungsbestands-, USDC-Kredit-/Rückzahlungs-, externen-Transfer- und Fiat-FX-Vollabgleich. Daher bleibt die Steuer-Quellabdeckung als unvollständig markiert.

Achte besonders auf `CYCLE_BLOCKED`, `NO_TRADE`, `RISK_GATE_REJECTED`, `CYCLE_FAILED_CIRCUIT_BREAKER`, `ORDER_OUTCOME_UNKNOWN_RECONCILING` und `LIVE_ORDER_BLOCKED_BY_RELEASE_GATE`. API-Key, Secret, Signatur oder Auth-Header gehören nicht in die Logs.

## Aktueller Freigabestatus

**Keine Live-Freigabe.** Der Source-Release-Schalter ist `False`, Konto-/Margin-Ledger-Reconciliation fehlt und dynamische Margin-Raten werden nicht ausreichend bestätigt. Erst nach grüner CI, vollständigen Integrations-/Recoverytests, realistischer historischen Prüfung und einem abgeschlossenen Forward-Shadow-Test ist eine neue explizite, geprüfte Freigabe zulässig. Die erste spätere Live-Stufe muss klein, beaufsichtigt und mit getesteter Schutz-/Recoverystrategie erfolgen.