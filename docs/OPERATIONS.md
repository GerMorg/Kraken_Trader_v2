# Betrieb auf Home Assistant OS

## Repository/App installieren

1. In Home Assistant **Einstellungen → Apps → App-Repository hinzufügen** öffnen.
2. `https://github.com/GerMorg/Kraken_Trader_v2` als benutzerdefiniertes Repository hinzufügen und den Store aktualisieren.
3. `Kraken Margin Trader` installieren bzw. über **Aktualisieren** auf Version `0.1.22` aktualisieren. Nach der Aktualisierung muss das Startprotokoll `version=0.1.22` zeigen; andernfalls läuft noch nicht das neue Image. Anschließend starten und das App-Protokoll prüfen.
4. `mode: SHADOW`, `live_enabled: false` und `kill_switch: true` beibehalten.
5. Für die erste Datenerfassung einen Kraken-API-Key mit den erforderlichen **Lese-Rechten** konfigurieren. Keine Auszahlungsrechte und zunächst keine Handelsrechte aktivieren.

## Containerstart in Version 0.1.18

Das Image prüft beim Build, ob `app/main.py` vorhanden und `app.main` importierbar ist. `run.sh` ist nun der Docker-`ENTRYPOINT`, nicht nur ein Standard-`CMD`; so bleibt die geprüfte Startlogik auch bei überschriebenen Container-Kommandos vorgeschaltet. CI startet das fertige Image ohne `--entrypoint`-Override mit `--healthcheck`, sodass die tatsächliche Image-Startkonfiguration getestet wird. Das vorhandene AppArmor-Profil war die eigentliche Zugriffslücke: Es erlaubte weder das Lesen von `/opt/kraken-trader/app/**` noch Schreibzugriff auf den gemappten `/config`-Reportpfad und ließ DNS-Datagramme nicht ausdrücklich zu. Version 0.1.18 ergänzt gezielte, nicht privilegierte Pfad-/Netzwerkregeln, behält AppArmor aktiviert und kompiliert das Profil im CI. CI führt den regulären `ENTRYPOINT` zusätzlich mit UID/GID `10001:10001` und einem temporären schreibbaren `/data` aus. Bei Fehlern bleibt die Diagnose robust und protokolliert UID/GID, Verzeichnis-/Dateimodi und einen sicheren Fehlerstatus, ohne dass das Auflisten eines unlesbaren Verzeichnisses die eigentliche Fehlermeldung verdeckt. Der Python-Bootstrap lädt das Paket weiter aus den expliziten Dateipfaden. Der Healthcheck ruft keine Kraken-API auf und sendet keine Orders.

## Zweck dieser Vorabversion

Diese Version soll nach Installation echte, aktuelle Konto-/Historien-Antworten einlesen, persistieren und Unterschiede sichtbar machen. Sie ist kein Live-Trading-Go. Grüne CI und ein erfolgreicher Container-Build belegen weder vollständige Kontenabstimmung noch Profitabilität.

Der Laufzeitpfad blockiert neue Entries weiterhin, solange einer der erforderlichen Nachweise fehlt: vollständige Trades-/Ledgers-/ClosedOrders-Historie, positionsbezogene Order-/Fill-/Ledger-Abstimmung, Wallet-/Kredit-/USDC-/Settlement-Abstimmung, belegte aktuelle Margin-Eröffnungs-/Rolloverkosten, valide tägliche PnL-/Drawdown-Baselines und ein vorab validierter kostenbereinigter Strategie-Edge. Der aktuelle Code setzt `edge_calibrated=False`; deshalb lässt sich die Entry-Freigabe nicht allein durch HA-Konfiguration einschalten.

## API-Key-Rechte und Sicherheit

Keine Auszahlungsberechtigung (`withdraw-funds`). Für die reine Datenerfassung müssen die von der Runtime verwendeten Query-Rechte für Konto-/Trade-Balance, offene und geschlossene Orders/Trades, Positionen und Ledger verfügbar sein. Die App fragt vor privaten History-Abfragen `GetApiKeyInfo` ab und prüft Berechtigungen, Ablaufzeit und History-Zeitfenster. Die Antwort wird auf erlaubte Metadaten reduziert; das Feld `apiKey` wird nicht in Audit-Ereignisse übernommen.

Wenn `GetApiKeyInfo` weiterhin unvollständige Metadaten meldet, protokolliert Version 0.1.22 die sicheren Feldnamen `api_key_info_response_fields_present`, `api_key_metadata_fields_present` und `api_key_metadata_fields_invalid`. Bei einer ungültigen Trade-History-Zeile nennt `last_error` die Validierungsursache und der Fingerprint erlaubt, denselben Eintrag über Zyklen hinweg zu erkennen; weder die Trade-ID noch rohe API-Antworten werden ausgegeben.

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
- `/data/kraken_trader.sqlite3`: persistente Historie, Intents, Snapshots und Reconciliation-Evidenz.
- `/config/reports/tax/AT/<Jahr>/`: JSON-, CSV- und Markdown-Steuerexporte im beschreibbaren App-Konfigurationsordner.

Nützliche Ereignisse sind `API_KEY_PERMISSION_GATE_BLOCKED`, `KRAKEN_HISTORY_SYNC`, `NEW_RISK_BLOCKED_HISTORY_INCOMPLETE`, `WALLET_BALANCE_RECONCILIATION_SUMMARY`, `ORDER_RECONCILIATION_SUMMARY`, `POSITION_FILL_RECONCILIATION_SUMMARY`, `NO_TRADE`, `RISK_GATE_REJECTED` und `CYCLE_FAILED_CIRCUIT_BREAKER`. API-Key, Secret, Signatur und Auth-Header gehören niemals in Logs oder GitHub-Issues.

## Freigabestatus

**Noch keine Empfehlung für Live-Trading, insbesondere keinen unbeaufsichtigten Margin-Handel.** Es gibt keinen zusätzlichen Compile-Time-Release-Schalter; dies ist ausdrücklich keine Live-Freigabe. Das aktuelle Entry-Gate blockiert neue Entries wegen unkalibrierter Edge-Evidenz und fehlender unabhängiger Portfolio-/Kosten-/PnL-Abnahmen. Außerdem werden Positions-Exit-Entscheidungen zurzeit als Shadow-Vorschläge protokolliert und nicht als echte Close-Orders gesendet. Erst wenn reale Account-/History-Daten ausgewertet, die Voll-Reconciliation implementiert und getestet, Kosten-/Edge-Nachweise erbracht und ein kontrollierter Recovery-/Exit-Pfad validiert sind, ist eine separate Änderung für einen beaufsichtigten Live-Test zu prüfen.
