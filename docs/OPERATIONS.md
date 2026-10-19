# Betrieb auf Home Assistant OS

## Version 0.1.34 — gezielter Trade-/Ledger-Fallback

Bei gespeicherten Trades ohne Ledger-Verknüpfung verwendet Version 0.1.34 zusätzlich eine gezielte Abfrage über Kraken TradesHistory. Dieser Fallback wird bei einem zuvor erfolglosen QueryTrades-Abruf sofort versucht; die frühere sechs-Stunden-Wartezeit blockiert ihn nicht. Die Anfrage nutzt ledgers=true, ein Zeitfenster von fünf Sekunden vor und nach dem gespeicherten Trade-Zeitpunkt und Cursor-Paginierung mit höchstens fünf Seiten. Es wird nur eine Antwort übernommen, deren Schlüssel exakt der erwarteten Trade-ID entspricht.

Im Ereignis KRAKEN_TRADE_LEDGER_RECONCILIATION sind dazu folgende Felder sichtbar: trade_rows_history_window_queried, trade_rows_history_window_pages, trade_rows_history_window_recovered, trade_rows_history_window_ledger_field_missing, trade_rows_history_window_not_found, trade_rows_history_window_deferred und next_trade_history_retry_epoch. Wenn die Relation weiterhin fehlt, bleibt der Quality-Gap aktiv. Leere Listen werden nicht allein wegen einer erfolgreichen API-Antwort akzeptiert, sondern benötigen weiterhin den vollständigen unabhängigen Ledger-/refid-Abgleich.

## Version 0.1.32 – bestehende HA-Mounts direkt verwenden

Version 0.1.31 hat nach den vorherigen Änderungen noch versucht, `/data/state/private` und `/data/logs/private` anzulegen. Auf den bestehenden, bereits auf UID/GID `10001:10001` und Modus `0700` gesetzten Mountverzeichnissen war das für den Root-Prozess nicht erlaubt. Version 0.1.32 legt deshalb keine Unterverzeichnisse unter `/data/state` und `/data/logs` an; der Trader nutzt diese Verzeichnisse direkt. Ihre Modi werden nicht geändert. Für den Migrationsschritt wird ihre Eigentümerschaft vorübergehend an root und vor dem Benutzerwechsel zurück an UID/GID `10001:10001` gegeben; Dateien auf der obersten Verzeichnisebene werden einzeln neu zugeordnet, ohne rekursiv in alte Unterverzeichnisse abzusteigen. Die Datenbank bleibt `/data/state/kraken_trader.sqlite3`, das AppLog `/data/logs/events.jsonl`. Aktualisiere auf Version `0.1.32` und prüfe im Startprotokoll `version=0.1.32` sowie `uid=10001 gid=10001`.
## Version 0.1.29 – Laufzeit als unprivilegierter Benutzer

Home Assistant kann den App-Container zunächst als root starten. `run.sh` nutzt diese Phase nur, um `/data/state`, `/data/logs` und `/config/reports/tax` für UID/GID `10001:10001` vorzubereiten. Das Verzeichnis `/data` selbst bleibt root-owned und für die App nicht beschreibbar; `/data/options.json` bleibt root-owned und unverändert. Eine temporäre Kopie unter `/run/kraken-trader-options.json` wird dem App-Benutzer mit Modus `0400` übergeben. Anschließend startet Python als `uid=10001 gid=10001`. Beim ersten Start ab 0.1.29 wird die bisherige SQLite-Datei `/data/kraken_trader.sqlite3` mitsamt vorhandenen `-wal`, `-shm`- und `-journal`-Dateien nach `/data/state/kraken_trader.sqlite3` migriert. Ein persistenter Migrationsmarker ermöglicht die kontrollierte Fortsetzung nach einem unterbrochenen Neustart; doppelte Datenbanken, Seitendatei-Kollisionen und verwaiste Dateien führen zu einem Fehler und werden nicht stillschweigend überschrieben. In der Startdiagnose muss `uid=10001 gid=10001` stehen. Wenn weiterhin `uid=0 gid=0` erscheint, läuft noch ein älteres Image oder der neue Startpfad wurde nicht aktiviert.

Der CI-Container-Test führt den normalen Entrypoint mit root-owned temporären `/data`- und `/config`-Mounts aus, prüft den UID-Wechsel und bestätigt, dass die App-Konfiguration geladen wurde. Ein separater Test prüft weiterhin den direkten Start mit einer vorgegebenen unprivilegierten UID. Beide sind Healthchecks und platzieren keine Kraken-Orders.

## Version 0.1.28 – kontrollierte erneute Abfrage fehlender Trade-Ledger-Referenzen

Ungeklärte Antworten ohne `ledgers`-Feld werden nach sechs Stunden erneut über `QueryTrades` geprüft. Das Ereignis `KRAKEN_TRADE_LEDGER_RECONCILIATION` enthält `trade_rows_query_deferred` und `next_trade_query_retry_epoch`; das ist ein Statushinweis, kein Nachweis vollständiger Reconciliation. Datenqualitätslücken bleiben bestehen und neue Risikoerhöhungen bleiben blockiert, bis eine unabhängige Quelle die Zuordnung bestätigt.

## Version 0.1.27 – fehlendes `ledgers`-Feld

Wenn `QueryTrades` eine gültige Trade-Zeile ohne `ledgers`-Feld liefert, persistiert die App jetzt die Antwort mit `QUERY_TRADES_LEDGER_FIELD_MISSING`. Das verhindert redundante Abfragen in jedem Zyklus, ohne die Historie als vollständig zu markieren. `missing_trade_references` und der History-Quality-Gap bleiben bestehen, bis eine unabhängige Quelle die Relation repariert. Ein gecachtes Diagnoseergebnis ist ausdrücklich kein Beleg für vollständige Reconciliation und erlaubt keine neuen Risikoerhöhungen.

## Repository/App installieren

1. In Home Assistant **Einstellungen → Apps → App-Repository hinzufügen** öffnen.
2. `https://github.com/GerMorg/Kraken_Trader_v2` als benutzerdefiniertes Repository hinzufügen und den Store aktualisieren.
3. `Kraken Margin Trader` installieren bzw. über **Aktualisieren** auf Version `0.1.32` aktualisieren. Nach der Aktualisierung muss das Startprotokoll `version=0.1.32` und die Python-Bootstrap-Diagnose `uid=10001 gid=10001` zeigen; andernfalls läuft noch nicht das neue Image oder der Privilegienwechsel ist fehlgeschlagen. Anschließend starten und das App-Protokoll prüfen.
4. `mode: SHADOW`, `live_enabled: false` und `kill_switch: true` beibehalten.
5. Für die erste Datenerfassung einen Kraken-API-Key mit den erforderlichen **Lese-Rechten** konfigurieren. Keine Auszahlungsrechte und zunächst keine Handelsrechte aktivieren.

## Trade-/Ledger-Abgleich in Version 0.1.25

Das strukturierte Ereignis `KRAKEN_TRADE_LEDGER_RECONCILIATION` zeigt die Anzahl der nachgefragten und wiederhergestellten Trade- und Ledger-Verknüpfungen sowie verbleibende Lücken. `KRAKEN_HISTORY_DATA_QUALITY_GAP_RESOLVED` protokolliert eine konkret reparierte Verknüpfung. `TRADE_LEDGER_COVERAGE_INCOMPLETE:ALL_TRADES` oder `LEDGER_HISTORY_COVERAGE_INCOMPLETE:EMPTY_FULL_BACKFILL` bedeutet, dass die Datenquellen noch nicht konsistent sind.

Die App nutzt Kraken-Cursor für die Ledger-Historie und fragt einzelne Trade-/Ledger-IDs begrenzt nach. Wenn sich der gesamte Ledger-Backfill als leer erweist, obwohl Trades vorhanden sind, wird er erneut gestartet. Diese Sperren nicht manuell aus der SQLite-Datenbank entfernen. Bis sämtliche Quellen und das Gesamtportfolio vollständig abgeglichen sind, bleiben `mode: SHADOW`, `live_enabled: false` und `kill_switch: true` aktiv.

## Diagnose leerer Trade-Ledger-Listen in Version 0.1.26

Im Ereignis `KRAKEN_TRADE_LEDGER_RECONCILIATION` zeigen `trade_query_outcomes` und die Zähler, ob eine Trade-ID bei `QueryTrades` fehlt, das `ledgers`-Feld fehlt, die Antwort ungültig ist oder Kraken eine leere Liste zurückgibt. Es werden nur kurze SHA-256-Fingerprints protokolliert, keine Trade-IDs.

Eine explizit leere Liste wird nur dann als geklärt behandelt, wenn der vollständige Trade- und Ledger-Backfill erfolgreich ist und kein Ledger-Eintrag mit derselben `refid` existiert. Wenn QueryTrades eine leere Liste meldet, aber der Ledger-Bestand eine passende `refid` enthält, wird `TRADE_LEDGER_REFERENCE_SOURCE_CONFLICT` protokolliert und die Sperre bleibt bestehen. Die Anzahl der bestätigten leeren Relationen steht in `trade_rows_confirmed_empty`.

## Containerstart in Version 0.1.18

Das Image prüft beim Build, ob `app/main.py` vorhanden und `app.main` importierbar ist. `run.sh` ist nun der Docker-`ENTRYPOINT`, nicht nur ein Standard-`CMD`; so bleibt die geprüfte Startlogik auch bei überschriebenen Container-Kommandos vorgeschaltet. CI startet das fertige Image ohne `--entrypoint`-Override mit `--healthcheck`, sodass die tatsächliche Image-Startkonfiguration getestet wird. Das vorhandene AppArmor-Profil war die eigentliche Zugriffslücke: Es erlaubte weder das Lesen von `/opt/kraken-trader/app/**` noch Schreibzugriff auf den gemappten `/config`-Reportpfad und ließ DNS-Datagramme nicht ausdrücklich zu. Version 0.1.18 ergänzt gezielte, nicht privilegierte Pfad-/Netzwerkregeln, behält AppArmor aktiviert und kompiliert das Profil im CI. CI führt den regulären `ENTRYPOINT` zusätzlich mit UID/GID `10001:10001` und einem temporären schreibbaren `/data` aus. Bei Fehlern bleibt die Diagnose robust und protokolliert UID/GID, Verzeichnis-/Dateimodi und einen sicheren Fehlerstatus, ohne dass das Auflisten eines unlesbaren Verzeichnisses die eigentliche Fehlermeldung verdeckt. Der Python-Bootstrap lädt das Paket weiter aus den expliziten Dateipfaden. Der Healthcheck ruft keine Kraken-API auf und sendet keine Orders.

## Zweck dieser Vorabversion

Diese Version soll nach Installation echte, aktuelle Konto-/Historien-Antworten einlesen, persistieren und Unterschiede sichtbar machen. Sie ist kein Live-Trading-Go. Grüne CI und ein erfolgreicher Container-Build belegen weder vollständige Kontenabstimmung noch Profitabilität.

Der Laufzeitpfad blockiert neue Entries weiterhin, solange einer der erforderlichen Nachweise fehlt: vollständige Trades-/Ledgers-/ClosedOrders-Historie, positionsbezogene Order-/Fill-/Ledger-Abstimmung, Wallet-/Kredit-/USDC-/Settlement-Abstimmung, belegte aktuelle Margin-Eröffnungs-/Rolloverkosten, valide tägliche PnL-/Drawdown-Baselines und ein vorab validierter kostenbereinigter Strategie-Edge. Der aktuelle Code setzt `edge_calibrated=False`; deshalb lässt sich die Entry-Freigabe nicht allein durch HA-Konfiguration einschalten.

## API-Key-Rechte und Sicherheit

Keine Auszahlungsberechtigung (`withdraw-funds`). Für die reine Datenerfassung müssen die von der Runtime verwendeten Query-Rechte für Konto-/Trade-Balance, offene und geschlossene Orders/Trades, Positionen und Ledger verfügbar sein. Die App fragt vor privaten History-Abfragen `GetApiKeyInfo` ab und prüft Berechtigungen, Ablaufzeit und History-Zeitfenster. Die Antwort wird auf erlaubte Metadaten reduziert; das Feld `apiKey` wird nicht in Audit-Ereignisse übernommen.

Version 0.1.23 erkennt `valid_until`, `query_from` und `query_to` neben den camelCase-Aliasnamen. Die Ereignisse `KRAKEN_API_KEY_PERMISSION_CHECK` zeigen sowohl vorhandene Response-Feldnamen als auch `api_key_metadata_source_fields` ohne Metadatenwerte oder API-Geheimnisse. Ein fehlendes Trade-`ledgers`-Feld stoppt den Seitenimport nicht mehr, erzeugt aber `KRAKEN_HISTORY_DATA_QUALITY_GAP` und blockiert neue Risikoerhöhungen über `HISTORY_DATA_QUALITY_GAP:TRADES:TRADE_LEDGER_REFERENCES_MISSING`. Der Fingerprint identifiziert den Datensatz über Zyklen hinweg, ohne die rohe Kraken-Trade-ID auszugeben. Der Gap wird automatisch entfernt, falls derselbe Datensatz bei einer späteren Synchronisierung mit einem validen `ledgers`-Feld zurückkommt.

Für die aktuelle Shadow-Datenerfassung sind keine Handelsrechte erforderlich. Handelsrechte erst nach separater Prüfung hinzufügen. Während der Installations-/Reconciliation-Phase `mode: LIVE` nicht aktivieren, `live_enabled: true` nicht setzen und den Kill-Switch nicht deaktivieren. Auch dann gilt: Ein Entry ist nur zulässig, wenn alle unabhängigen Daten-, Kosten-, Edge- und Risiko-Gates bestanden sind.

## Einstellungen

- `mode`: `SHADOW` ist der empfohlene Prüfmodus; `PAPER` ändert nichts an der fehlenden Echtgeldfreigabe.
- `live_enabled`: im sicheren Erstbetrieb `false` belassen.
- `kill_switch`: sicheren Default `true` belassen.
- Scanintervall, Datenalter, Kandidatenzahl und Trend-Schwellen sind nur innerhalb des Schemas konfigurierbar.
- Risikogrenzen können nicht über die im Source-Code gesetzten Höchstwerte angehoben werden.
- Keine HA-Sensoren oder GUI erforderlich; Logs und persistente Ereignisse sind die primäre Betriebsoberfläche.

## Logs und persistente Daten

- Home-Assistant-App-Log: Laufzeitereignisse.
- `/data/logs/events.jsonl`: rotierende strukturierte UTC-Auditspur.
- `/data/state/kraken_trader.sqlite3`: persistente Historie, Intents, Snapshots und Reconciliation-Evidenz.
- `/config/reports/tax/AT/<Jahr>/`: JSON-, CSV- und Markdown-Steuerexporte im beschreibbaren App-Konfigurationsordner.

Nützliche Ereignisse sind `API_KEY_PERMISSION_GATE_BLOCKED`, `KRAKEN_HISTORY_SYNC`, `NEW_RISK_BLOCKED_HISTORY_INCOMPLETE`, `WALLET_BALANCE_RECONCILIATION_SUMMARY`, `ORDER_RECONCILIATION_SUMMARY`, `POSITION_FILL_RECONCILIATION_SUMMARY`, `NO_TRADE`, `RISK_GATE_REJECTED` und `CYCLE_FAILED_CIRCUIT_BREAKER`. API-Key, Secret, Signatur und Auth-Header gehören niemals in Logs oder GitHub-Issues.

## Freigabestatus

**Noch keine Empfehlung für Live-Trading, insbesondere keinen unbeaufsichtigten Margin-Handel.** Es gibt keinen zusätzlichen Compile-Time-Release-Schalter; dies ist ausdrücklich keine Live-Freigabe. Das aktuelle Entry-Gate blockiert neue Entries wegen unkalibrierter Edge-Evidenz und fehlender unabhängiger Portfolio-/Kosten-/PnL-Abnahmen. Außerdem werden Positions-Exit-Entscheidungen zurzeit als Shadow-Vorschläge protokolliert und nicht als echte Close-Orders gesendet. Erst wenn reale Account-/History-Daten ausgewertet, die Voll-Reconciliation implementiert und getestet, Kosten-/Edge-Nachweise erbracht und ein kontrollierter Recovery-/Exit-Pfad validiert sind, ist eine separate Änderung für einen beaufsichtigten Live-Test zu prüfen.
