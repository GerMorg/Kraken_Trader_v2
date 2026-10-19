# Changelog

## 0.1.36 — Interne Live-Freigabe wird technisch erzwungen

- Die zentrale `OrderAuthority` blockiert echte Orderübermittlung, solange `INTERNAL_LIVE_EXECUTION_RELEASED` im Code `False` ist. Home-Assistant-Optionen allein können die Sperre nicht aufheben.
- Regressionstest beweist, dass bei aktivierten Live-Operator-Schaltern kein Kraken-Orderaufruf erfolgt, und erhält einen separaten Test des echten Ausführungspfads unter explizit freigegebenem Test-Flag.
- `KRAKEN_HISTORY_RECONCILED` protokolliert den endgültigen Zustand nach gezielten Reconciliation-Reparaturen; `CYCLE_SUMMARY` enthält Historienbereitschaft und tatsächlichen Live-Release-Status.
- Diese Version bleibt ausdrücklich nicht für unbeaufsichtigte Live-Ausführung freigegeben.

## 0.1.35 — Kraken-Trade-Export als Ledger-Recovery-Fallback

- Wenn sowohl QueryTrades als auch TradesHistory die Ledger-Verknüpfung eines gespeicherten Trades nicht liefern, fordert die App einen eng begrenzten Kraken-Trades-Export an (15 Sekunden vor/nach dem gespeicherten Trade-Zeitpunkt).
- Die asynchronen AddExport-/ExportStatus-/RetrieveExport-Schritte werden über die persistierte Export-ID nach Neustarts fortgesetzt. Die heruntergeladene ZIP-/CSV-Datei wird nur im Arbeitsspeicher verarbeitet.
- Übernommen werden Ledger-IDs nur aus genau einer Exportzeile mit exakt übereinstimmender txid und zusätzlichen Prüfungen von Order-ID, Zeit, Richtung, Preis, Volumen und Gebühr. Doppelte, widersprüchliche, unvollständige oder ungültige Antworten bleiben blockiert.
- Ein leerer Ledger-Eintrag wird weiterhin erst nach dem unabhängigen vollständigen Ledger-/refid-Abgleich bestätigt. Erfolgreich wiederhergestellte Exporte werden über RemoveExport gelöscht.
- Neue Diagnosefelder trade_rows_export_* und next_trade_export_retry_epoch zeigen Anfragen, Statusprüfungen, Wiederherstellungen, Wartezustände und Fehler. Keine Änderung an der Live-Freigabe.

## 0.1.34 — Gezielte TradesHistory-Reparatur für fehlende Ledger-Verknüpfungen

## 0.1.34 — Gezielte TradesHistory-Reparatur für fehlende Ledger-Verknüpfungen

- Wenn eine QueryTrades-Antwort das ledgers-Feld nicht enthält, wechselt die App sofort zu einer gezielten Abfrage über TradesHistory. Sie wiederholt nicht nur denselben Abruf nach sechs Stunden.
- Der Fallback nutzt ledgers=true, ein enges Zeitfenster um den gespeicherten Börsen-Zeitstempel und Kraken-Cursor-Paginierung. Nur die exakt übereinstimmende Trade-ID darf den vorhandenen Datensatz ersetzen.
- Leere Ledger-Listen werden weiterhin erst nach unabhängigem Abgleich mit der vollständigen Ledger-Historie und den refid-Verknüpfungen bestätigt. Fehlende, ungültige oder nicht gefundene Daten bleiben gesperrt.
- Neue Diagnosefelder trade_rows_history_window_* zeigen Abfragen, gelesene Seiten, Wiederherstellungen, fehlende Felder und den nächsten Wiederholungszeitpunkt. Keine Änderung an der Live-Freigabe.

## 0.1.33 — History-Qualitätslücken auch in der Positions-Reconciliation sperren

- Die Positions-/Fill-Reconciliation behandelt verbleibende `quality_gaps` jetzt wie eine unvollständige Historie, auch wenn die Pagination technisch abgeschlossen ist.
- Das strukturierte Ereignis weist betroffene Quellen unter `history_quality_gap_sources` aus und verwendet `HISTORY_DATA_QUALITY_GAP` statt fälschlich `history_ready: true` zu melden.
- Regressionstest für abgeschlossene Synchronisierung mit ungelöster Trade-/Ledger-Abdeckungslücke; keine Änderung an Orderausführung oder Freigabe von Live-Trading.

## 0.1.32 — Vorhandene HA-Mountverzeichnisse direkt verwenden

- Behebt `mkdir: can't create directory '/data/state/private': Permission denied`: Es werden keine zusätzlichen Verzeichnisse unter den gemounteten `/data/state`- und `/data/logs`-Pfaden mehr erstellt.
- Verwendet die bisherigen stabilen Pfade `/data/state/kraken_trader.sqlite3` und `/data/logs/events.jsonl`. Es gibt weder einen `chmod`-Aufruf auf `/data` noch rekursive Verzeichnisdurchläufe in möglichen alten, nicht durchsuchbaren Unterverzeichnissen.
- Der Entrypoint übernimmt die Eigentümerschaft der existierenden Mount-Verzeichnisse für den kontrollierten Migrationsschritt und gibt sie danach an UID/GID `10001:10001` zurück. Die vorhandenen Modusbits werden nicht verändert. SQLite-Datenbank, WAL-/SHM-/Journal-Dateien und Legacy-Migrationsschutz bleiben erhalten.
- CI präpariert State-/Log-Mounts als UID/GID `10001:10001` mit Modus `0700`, entfernt `DAC_OVERRIDE` und `FOWNER` aus dem Container und verbietet `chmod` auf persistenten `/data`-Pfaden.

## 0.1.31 — Kein `chmod` auf HA-Mountverzeichnissen

- Behebt den erneut beobachteten Startabbruch `chmod: /data/state: Operation not permitted`. Die vorherige Korrektur führte `chmod` weiterhin auf dem eingebundenen `/data`-Volume aus und war daher für das tatsächliche Home-Assistant-Mountverhalten nicht robust genug.
- Der Entrypoint erstellt `/data/state/private` und `/data/logs/private` mit `umask 077`; auf `/data`, `/data/state`, `/data/logs` und andere gemountete Verzeichnisse wird kein `chmod` ausgeführt. AppArmor bleibt aktiviert und `/data` muss für UID/GID `10001:10001` weiterhin nicht beschreibbar sein.
- SQLite wird zuerst aus dem alten Pfad `/data/kraken_trader.sqlite3` in den bisherigen Pfad überführt und anschließend in `/data/state/private/kraken_trader.sqlite3` verschoben. WAL-, SHM- und Journal-Dateien werden mitgenommen; widersprüchliche Datenbanken oder Seitendateien blockieren den Start.
- Der AppLog liegt künftig unter `/data/logs/private/events.jsonl`. Container- und Strukturtests prüfen neue Pfade, restriktive Verzeichnisanlage, Privilegienwechsel und Datenbankerhalt.

## 0.1.30 — Rechteänderung auf HA-Mounts ohne Root-CAP_FOWNER

- Behebt den Startabbruch `chmod: /data/state: Operation not permitted` und die entsprechende Meldung für `/data/logs`.
- Nach dem `chown` wird `chmod -R go-rwx` mit `su-exec` als UID/GID `10001:10001` ausgeführt. Damit ändert der Verzeichniseigentümer die Rechte, statt sich auf Root-Capabilities zu verlassen, die Home Assistant beim Containerstart einschränken kann.
- Der Trader läuft weiterhin ausschließlich als unprivilegierter Benutzer; die sichere Abbruchprüfung für Datenbank, AppLog und Steuerexportpfad bleibt aktiv.
- Tests und HA-Strukturvalidator verlangen ausdrücklich, dass die Rechteänderung als Eigentümer ausgeführt wird. Der vollständige CI-Container-Smoke-Test deckt den Standardstart samt Datenbankmigration ab.

## 0.1.29 — Standardstart als unprivilegierter Benutzer

- Das HA-Container-Entrypoint bereitet nur `/data/state`, `/data/logs` und `/config/reports/tax` für UID/GID `10001:10001` vor und startet danach den Python-Prozess als diesen unprivilegierten Benutzer.
- `/data` selbst und die von Home Assistant verwaltete `/data/options.json` bleiben root-owned; die App kann die Optionsdatei weder lesen noch über die beschreibbare Elternablage austauschen. Sie liest eine temporäre Kopie mit Modus `0400` unter `/run/kraken-trader-options.json`.
- Die bisherige Datenbank `/data/kraken_trader.sqlite3` und vorhandene SQLite-Seitendateien werden in `/data/state/kraken_trader.sqlite3` migriert. Ein persistenter Marker ermöglicht die sichere Fortsetzung einer unterbrochenen Migration. Bei doppelten Datenbanken, kollidierenden Seitendateien oder verwaisten Altdateien startet die App nicht, statt einen Bestand zu überschreiben oder still auszuwählen.
- Der AppArmor-Policy werden ausschließlich die für den Besitzwechsel nötigen Fähigkeiten und der `su-exec`-Übergang ergänzt; die breite Dateifähigkeit bleibt verboten.
- CI prüft den Standardstart mit root-owned Daten-Mount, überprüft die resultierende UID/GID, das unveränderte Optionsdatei-Rechtemodell, die Konfigurationsaufnahme und den Erhalt eines SQLite-Sentinels nach der Migration.
- Wenn Mount-Rechte nicht sicher vorbereitet werden können, scheitert der Start, statt still als root weiterzulaufen.

## 0.1.28 — Geplante erneute Prüfung fehlender Trade-Ledger-Referenzen

- Persistierte `QueryTrades`-Antworten ohne `ledgers`-Feld werden nicht mehr unbegrenzt nur als Cache behandelt: Die App versucht nach sechs Stunden erneut, die optionale Relation abzufragen.
- Jeder Versuch speichert den Versuchszähler, den letzten Abfragezeitpunkt, das Ergebnis und den nächsten Prüfzeitpunkt. Fehlgeschlagene oder unvollständige Antworten werden ebenfalls gedrosselt.
- `KRAKEN_TRADE_LEDGER_RECONCILIATION` zeigt nun, wie viele Abfragen wegen des Backoff-Fensters zurückgestellt wurden und wann die nächste erneute Prüfung fällig ist.
- Ungeklärte Beziehungen bleiben Datenqualitätslücken und sperren neue Risikoerhöhungen. Kein Ledger wird geraten oder erfunden; das ist keine Live-Freigabe.
- Regressionstests decken Abkühlzeit, Persistenz, erneute Abfrage und erfolgreiche unabhängige Auflösung ab.

## 0.1.27 — Fehlendes Trade-Ledger-Feld persistent diagnostizieren

- Eine gültige `QueryTrades`-Antwort ohne `ledgers`-Feld wird nun mit einem expliziten Diagnose-Fingerprint und `QUERY_TRADES_LEDGER_FIELD_MISSING` persistiert.
- Wiederholte identische ID-Abfragen werden vermieden, solange die fehlende Relation nicht durch eine unabhängige Quelle geklärt wurde.
- Die Reconciliation bleibt `INCOMPLETE`; Qualitätslücken und die Sperre für neue Risikoerhöhungen bleiben bestehen. Kein Feld wird erfunden und keine Live-Freigabe erteilt.
- Regressionstest deckt Persistenz, idempotente Wiederholung und unveränderte Sperre ab.


## 0.1.26 — Explizit leere Trade-Ledger-Relationen sicher prüfen

- Der bisherige Logstatus zeigte drei Abfragen über `QueryTrades`, aber null wiederhergestellte Beziehungen und keine Erklärung, ob Kraken die IDs nicht zurückgab, das `ledgers`-Feld fehlte oder eine leere Liste zurückkam.
- Die Reconciliation protokolliert nun diese Ausgänge getrennt und ausschließlich mit nicht umkehrbaren Trade-ID-Fingerprints.
- Eine explizit leere `ledgers`-Liste wird erst dann als geklärt persistiert, wenn Trades- und Ledger-Historien vollständig sind und kein Ledger-Eintrag mit derselben Trade-`refid` existiert. Taucht eine solche Ledger-Referenz dennoch auf, entsteht ein harter Quellenkonflikt und neue Risikoerhöhungen bleiben gesperrt.
- Fehlende API-Antworten, fehlende Ledger-Felder, ungültige Zeilen und nicht reparierbare Referenzen bleiben unvollständig. Keine leeren Felder oder IDs werden künstlich ergänzt, und keine Sperren werden nur aufgrund einer erfolgreichen HTTP-Antwort entfernt.
- Versionsmetadaten, Dokumentation und Regressionstests aktualisiert. Keine Live-Freigabe.

## 0.1.25 — Quellübergreifende Trade-/Ledger-Reconciliation
     
- Der Ledgers-Import verwendet nun die dokumentierte cursorbasierte Paginierung statt des veralteten Offset-Verfahrens und sendet keinen veralteten `aclass`-Filter mehr.
- Fehlende oder leere Trade-Ledger-Referenzen werden begrenzt über `QueryTrades` nachgefragt. Referenzierte Ledger-Datensätze werden über `QueryLedgers` in Batches von höchstens 20 IDs nachgeladen.
- Nach Abschluss der vollständigen Ledger-Historie gleicht die App Trade-Verknüpfungen anhand des von Kraken gelieferten Ledger-`refid` ab. Abgeleitete Zuordnungen bleiben im Datensatz mit ihrer Belegquelle gekennzeichnet.
- Ein scheinbar vollständiger, aber leerer Ledger-Backfill widerspricht vorhandenen Trade-Daten: Die App persistiert einen Coverage-Konflikt, startet den vollständigen Ledger-Import erneut und hält neue Risikoerhöhungen gesperrt.
- Logs unterscheiden den Fortschritt des eigentlichen initialen Backfills von weiterhin offenen Datenqualitätslücken. Erweiterte Regressionstests decken Cursor-Paginierung, gezielte Nachabfragen, `refid`-Reparatur und leere Ledger-Backfills ab. Das ist keine Live-Freigabe.

## 0.1.24 — Fiat-Basismärkte aus dem Krypto-Universum ausschließen

- Die AssetPair-Auswahl akzeptiert weiterhin nur Online-Märkte mit USD-Quote, schließt jetzt aber zusätzlich Fiat-Basiswährungen aus. Kraken klassifiziert sowohl reguläre Krypto-Assets als auch viele Fiat-Währungen mit `aclass_base: currency`; diese Klassifizierung allein reicht deshalb nicht, um einen Markt als Kryptowährung einzustufen.
- Die Filterung prüft sowohl `wsname`-Basissymbole als auch Kraken-interne Base-Asset-IDs (zum Beispiel `ZEUR`/`ZGBP`). Regressionstests belegen, dass `EUR/USD`, `GBP/USD` und `CAD/USD` verworfen werden, während XBT/USD und ein neues dynamisch entdecktes Krypto/USD-Paar zugelassen bleiben.
- Keine Coin-Whitelist; keine Veränderung am sicheren Shadow-/Reconciliation-Gate.

## 0.1.23 — Metadaten-Aliase und fortsetzbarer History-Import

- `GetApiKeyInfo`-Metadaten werden sowohl als dokumentierte camelCase-Felder (`validUntil`, `queryFrom`, `queryTo`) als auch in der im Live-Log tatsächlich beobachteten snake_case-Form (`valid_until`, `query_from`, `query_to`) erkannt. Bei widersprüchlichen Aliaswerten gilt das Feld als ungültig und wird nicht freigegeben.
- Ein Trade-Datensatz ohne `ledgers`-Array blockiert nicht mehr dauerhaft das Voranschreiten des Backfills. Der Datensatz wird mit expliziter Kennzeichnung fehlender Ledger-Verknüpfung importiert; ein persistierter Fingerprint-Datenqualitäts-Gap blockiert weiterhin neue Risikoerhöhungen.
- SQLite migriert bestehende `history_sync_state`-Tabellen automatisch um ein Feld für persistente Qualitätslücken. Ein Gap bleibt über Neustarts und zwischengespeicherte Synchronisierungen hinweg erhalten und kann automatisch entfernt werden, wenn derselbe Trade später mit gültigem `ledgers`-Feld erneut eingelesen wird.
- Tests decken snake_case-Metadaten, widersprüchliche Aliaswerte, Migration/Persistenz der Qualitätslücken und die Trennung zwischen fortsetzbarem Import und gesperrtem neuen Risiko ab.

## 0.1.22 — Einheitlicher JSON-Transport und präzise History-Diagnose

- Alle privaten Kraken-Spot-REST-Anfragen verwenden den dokumentierten JSON-Transport. JSON-Boolesche Werte bleiben Boolesche Werte; die Signatur basiert auf exakt dem kompakten JSON-Body einschließlich Nonce.
- Die sichere `GetApiKeyInfo`-Diagnose gibt Feldnamen aus, darunter vorhandene, fehlende und ungültige Metadatenfelder, jedoch keine Feldwerte, API-Keys oder andere Geheimnisse.
- Trade-History-Validierungsfehler enthalten einen konkreten Fehlercode und einen kurzen SHA-256-Fingerprint der Trade-ID; ungültige Seiten werden weiterhin nicht als vollständig importiert oder im Cursor fortgeschrieben.
- Regressionstests prüfen JSON-Serialisierung und Signatur, Geheimnisschutz sowie die präzisen Ursachen fehlerhafter Trade-Einträge.

## 0.1.21 — GetApiKeyInfo mit dem dokumentierten JSON-Protokoll

- Der offizielle Kraken-Endpunkt `GetApiKeyInfo` erwartet laut aktueller Dokumentation `application/json`; zuvor wurde er fälschlich mit dem generischen Form-Transport angesprochen.
- Der Client sendet für diesen Endpunkt jetzt einen JSON-Body und erzeugt `API-Sign` anhand des exakten JSON-Texts einschließlich Nonce. Die übrigen privaten Endpunkte bleiben bei ihrem bestehenden Form-Transport.
- Ein Regressionstest prüft Request-Body, Content-Type, Accept-Header, Signatur und das Vorhandensein von `validUntil`, `queryFrom` und `queryTo` im simulierten API-Ergebnis.


## 0.1.20 — Kraken-API-Kompatibilität und Metadaten-Fallback

- Der private REST-Transport serialisiert signierte Requests pro Client und schreibt Boolesche Formwerte als `true`/ `false`, bevor Nonce, Signatur und Request-Body erzeugt werden.
- `GetApiKeyInfo` unterscheidet fehlende `validUntil`, `queryFrom` oder `queryTo`-Metadaten von einer tatsächlich abgelaufenen API-Key-Gültigkeit oder einem ausdrücklich eingeschränkten History-Zeitfenster.
- Fehlen Metadaten, prüft die Runtime die wirklichen History-Endpunkte. Fehler werden je Quelle protokolliert und halten neue Risikoerhöhungen gesperrt, ohne die aktuelle Konto-/Positionsabfrage unnötig abzubrechen.
- Regressionstests decken Boolesche Formserialisierung, unbekannte Metadaten, echten History-Fallback und explizite Query-Fensterrestriktionen ab.


## 0.1.19 — Aktuelle Positionsüberwachung trotz unvollständiger History-Metadaten

- GetApiKeyInfo-Metadatenlücken oder begrenzte historische Abfragefenster sperren weiter neue Risikoerhöhungen und den historischen Backfill, blockieren aber nicht mehr automatisch aktuelle Balance-, OpenOrders- und OpenPositions-Abfragen.
- ClosedOrders- und Ledger-History-Endpunkte werden nicht angerufen, wenn Ablauf-/History-Metadaten unbekannt oder eingeschränkt sind; die Reconciliation bleibt dabei explizit unvollständig.
- Regressionstests stellen sicher, dass aktuelle Positionen weiter zur Überwachung gelesen werden und neue Risikoerhöhungen bei fehlendem History-Nachweis blockiert bleiben.


## 0.1.18 — AppArmor-Pfade, DNS und Steuerreportzugriff

- Die v0.1.17-Logs zeigen normale Unix-Modi (`0755`/`0644`) und UID 0, aber `PermissionError` beim Öffnen von `/opt/kraken-trader/app/__init__.py`. Das bestehende AppArmor-Profil erlaubte den Zugriff auf diesen Anwendungsbaum nicht.
- Ergänzt read-only Traversierung/Lesen von `/opt/kraken-trader/app/**`; AppArmor bleibt aktiviert, `full_access` und pauschale Datei-Freigaben werden nicht verwendet.
- Ergänzt die fehlenden DNS-Datagrammregeln für IPv4/IPv6 und die Schreibrechte für den dokumentierten `/config/reports/tax/AT/<Jahr>/`-Exportpfad.
- CI validiert notwendige Profilregeln, kompiliert `apparmor.txt` mit `apparmor_parser --skip-kernel-load` und prüft weiter den Start unter einer unprivilegierten UID.

## 0.1.17 — Laufzeit-Dateirechte und nicht privilegierter Starttest

- Die HA-Logs von 0.1.16 zeigen `PermissionError: [Errno 13] Permission denied: '/opt/kraken-trader/app'` im Diagnosecode. Der unmittelbare Fehler war das fehlende Verzeichnis-Leserecht für den laufenden Containerprozess.
- Der Image-Build setzt das Anwendungspaket rekursiv auf lesbare Dateien und les-/durchsuchbare Verzeichnisse (`chmod -R a+rX`).
- Die Startdiagnose kann fehlgeschlagene Verzeichnisauflistung jetzt als Datenwert protokollieren, anstatt mit einem neuen `PermissionError` abzubrechen. UID/GID und Datei-/Verzeichnismodi werden mit ausgegeben.
- CI startet den Standard-Entrypoint zusätzlich unter UID/GID 10001 mit einem nur für den Test temporär bereitgestellten beschreibbaren `/data`. Das prüft die Dateirechte ohne Live-API-Aufrufe oder Orderübermittlung.

## 0.1.16 — Paketdateien direkt bootstrappen und beim Build starten

- Die Home-Assistant-Logs von 0.1.15 zeigten weiterhin `ModuleNotFoundError: No module named 'app'`, obwohl Python `/opt/kraken-trader` als `sys.path[0]` ausgab.
- Der Entrypoint lädt `app` jetzt direkt über eine Python-Modulspezifikation aus `app/__init__.py`, legt dessen Paketpfad fest und lädt `app.main` aus der konkreten Datei. Dadurch wird die fehlschlagende Top-Level-Paketauflösung umgangen.
- Der Bootstrap protokolliert Dateiexistenz, Verzeichnisinhalt und Lesbarkeit bei Fehlern.
- Das Dockerfile führt `/run.sh --healthcheck` bereits während des Image-Builds aus. Der genaue Anwendungseinstieg wird damit nicht nur importiert, sondern tatsächlich gestartet.

## 0.1.15 — Python-Modulpfad im Prozess explizit setzen

- Reagiert auf den bestätigten HA-Log von 0.1.14: Die Dateien wurden vom Shell-Wrapper gefunden, aber `python -m app.main` konnte `app` zur Laufzeit weiterhin nicht auflösen.
- Startet die Anwendung über einen Python-Bootstrap, der `/opt/kraken-trader` direkt in `sys.path` einfügt, bevor `app.main` importiert wird. Das Bootstrap-Protokoll meldet Executable, Arbeitsverzeichnis und `sys.path[0]`.
- Der Image-Build prüft sowohl den normalen Paketimport als auch den expliziten Bootstrap-Pfad. CI startet das Image weiterhin ohne Entrypoint-Override.

## 0.1.14 — tatsächlichen HA-Containerstart absichern

- `run.sh` wird als Docker-`ENTRYPOINT` erzwungen, damit die geprüfte Startlogik nicht nur von einem optionalen Standardkommando abhängt. Ein früheres direktes `python -m app.main`-Kommando wird sicher normalisiert.
- Die Runtime protokolliert beim Start Version, Python-Version und Executable; der Wrapper protokolliert Container-Root und `PYTHONPATH`, damit fehlende oder veraltete Images klarer identifizierbar sind.
- Kritischer CI-Fix: Der bisherige Smoke-Test setzte `--entrypoint /run.sh` ausdrücklich und umging damit die Image-Startkonfiguration. Der Test startet jetzt das Image ohne Entrypoint-Override. Validator und Regressionstest verhindern, dass dieser Override zurückkehrt.

## 0.1.13 — Container-Start und Import-Smoke-Tests

- Behebt den Startfehler `ModuleNotFoundError: No module named 'app'`, indem `run.sh` den erwarteten Paketpfad explizit prüft, `PYTHONPATH` setzt, in das Application-Root wechselt und Argumente an Python weitergibt.
- Das Dockerfile bricht schon beim Image-Build ab, wenn `app/main.py` oder `app/__init__.py` fehlt oder `app.main` nicht importierbar ist.
- CI führt nach dem Build nun tatsächlich `/run.sh --healthcheck` im Container aus. Damit werden Imports, Settings-/Datenbank-Initialisierung und die Runtime-Entrypointkette getestet; der Healthcheck ruft keine Kraken-API auf und sendet keine Orders.
- Die Strukturprüfung und Regressionstests prüfen Paket-Copy-Pfad, Entrypoint, Python-Suchpfad und Argumentweitergabe.
- Versionen in HA-Manifest, Python-Paket, Projektmetadaten, README und Docker-Build-Argumenten synchron auf `0.1.13` erhöht.


## 0.1.12 — Installations- und Datenprüfungs-Vorabversion

- Home-Assistant-App-Mapping auf den App-Konfigurationstyp `app_config` umgestellt; Docker-Build ergänzt um die Supervisor-Metadaten `io.hass.version`, `io.hass.type` und `io.hass.arch`.
- Österreichische Steuerexporte liegen im beschreibbaren App-Konfigurationsordner unter `/config/reports/tax/AT/<Jahr>/`; SQLite und rotierende Audit-Logs verbleiben im persistenten `/data`-Verzeichnis.
- Strukturprüfung und Regressionstest decken Mapping, Supervisor-Metadatenlabels und Versionskonsistenz ab.
- Dokumentation korrigiert, damit der entfernte Compile-Time-Release-Schalter nicht weiter als vorhanden beschrieben wird. Dies ist eine Shadow-/Datenprüfungs-Vorabversion, keine Live-Freigabe: unkalibrierter Edge, fehlende PnL-/Drawdown-Baselines, offene Voll-Reconciliation und unbestätigte Margin-Kosten blockieren weiterhin reguläre Entries.
- Die echten Account-/History-Daten sollen nach der Installation kontrolliert geprüft werden; CI verwendet ausschließlich simulierte Adapter und führt keine echten Orders aus.


## 0.1.11 — API-Key-Preflight und fail-closed Historienfreigabe

- Jede authentifizierte Runtime prüft über Kraken `GetApiKeyInfo` die tatsächlich erteilten Leserechte, Key-Ablaufzeit und den erlaubten History-Zeitraum.
- API-Key-Informationen werden auf erlaubte Metadaten reduziert; der mitgelieferte Key-Wert wird niemals in Logs oder Assessment-Ergebnisse übernommen.
- Neue Risikoerhöhungen werden bei fehlenden Query-Scopes, eingeschränkter/unklarer Historienabfrage, Ablauf-/Metadatenfehlern, unerlaubten Withdrawal-Rechten oder fehlender Berechtigung für angeforderte reine Ordervalidierung blockiert.
- Bekannte Scope-/Konfigurationsfehler werden zyklisch neu geprüft, statt allein dafür einen unauflösbaren permanenten Circuit Breaker zu setzen. Wenn private Leserechte fehlen, wird vor dem History-/Account-Snapshot-Pfad mit explizitem Blockierungsstatus abgebrochen; vorhandene Positionen können bei fehlendem Lesezugriff nicht zuverlässig überwacht werden.
- Die Historienfreigabe verlangt weiterhin vollständige, fehlerfreie Trades-, Ledger- und Closed-Orders-Backfills. Die bestehende Konten-, Finanzierung-, Recovery-, OOS-/Forward- und HA-OS-Abnahme bleibt offen; keine Live-Freigabe.
- Regressionstests prüfen vollständige und fehlende API-Scopes, Withdrawal-Rechte, Ablauf und History-Fenster, Secret-Redaction, angeforderte Orderrechte und Runtime-Abbruch ohne persistenten Konfigurations-Breaker.


## 0.1.10 — Fail-closed Historienfreigabe

- Neue Risikoerhöhungen werden blockiert, solange nicht alle drei erforderlichen Exchange-Historien (Trades, Ledgers und geschlossene Orders) vollständig importiert sind und der aktuelle Sync fehlerfrei abgeschlossen wurde.
- Fehlende, doppelte, unbekannte oder fehlerhafte History-Quellen haben explizite Blockierungsgründe; ein abgeschlossenes initiales Backfill allein reicht nicht, wenn der aktuelle Refresh noch läuft.
- Die Sperre gilt für neue Entries; die bestehende Positionsüberwachung und protokollierte Risikoreduktionsentscheidungen werden nicht durch diesen History-Gate ersetzt oder deaktiviert.
- Regressionstests prüfen vollständige, fehlende, doppelte, unvollständige und fehlerhafte History-Quellen.
- `GetApiKeyInfo` prüft die realen API-Key-Leserechte, Ablaufzeit und History-Zeitfenster; unnötige Auszahlungsrechte verhindern neue Entries. Die Antwort wird secret-sicher bereinigt und wiederkehrende Konfigurationsfehler werden im nächsten Zyklus erneut geprüft, statt einen veralteten permanenten Breaker zu hinterlassen.
- Zusätzliche Regressionstests prüfen fehlende Leserechte, unerlaubte Withdraw-Scope, fehlende Order-Berechtigungen und die Wiederherstellung nach Korrektur des API-Key-Zeitfensters.
- Diese Ergänzung schließt keine offenen Echtgeld-Konto-, Margin-Funding-, Settlement-, historischen Out-of-Sample-, Forward-Shadow- oder HA-OS-Abnahmekriterien. Live-Ausführung bleibt gesperrt.


## 0.1.9 — Nachweisbare Margin-Kosten und konservative Teilfüllungsbehandlung

- Margin-Kosten im Orderbuch-Replay werden nicht mehr als anonyme Summenzahl angenommen. Das neue `MarginCostEvidence` hält Eröffnungs- und Rolloverkosten getrennt, einschließlich Quote-Währung, Quelle, Quellen-ID, Zeitstempel und Qualitätskennzeichnung.
- Eine fehlende Referenz für Rollover-Kosten (auch bei Betrag null), Währungsabweichung oder Rollover nach dem Exit wird zurückgewiesen. Herkunftsmetadaten werden bis zu den Replay-Trade-Ergebnissen weitergereicht; diese Metadaten authentifizieren die Quelle nicht automatisch.
- Ein Einstieg mit nur teilweiser sichtbarer Ausführung wird im Szenario-Replay nicht als vollständiger Trade ausgegeben, solange der nicht ausgeführte Restauftrag bzw. seine Stornierung nicht simuliert wird.
- OHLCV-Simulator und Orderbuch-Replay schließen keine Position auf demselben Zeitstempel wie ihren Einstieg; ein Regressionstest schützt diesen zeitlichen Guard.
- EEA-/Margin-Dokumentation wurde erneut anhand der offiziellen Kraken-Dokumentation geprüft. Tatsächliche Kredit-, Gebühren-, USDC- und Ledger-Zuordnung ist im Runtimepfad weiterhin nicht vollständig integriert/verifiziert. Keine Live-Freigabe.
- Der Backtest weist Handelsgebühren, Spread, Depth-/Slippage-Impact und Margin-Kosten getrennt aus; Mid-Market-Brutto-PnL und All-in-Kosten müssen mathematisch zum Netto-PnL überleiten. Doppelte Spread-/Slippage-Abzüge werden durch Regressionstests verhindert.

## 0.1.8 — Orderbuch-Replay und sicherheitsgerichtete Tests

- Ergänzt einen deterministischen L2-Book-Walk für BUY/SELL mit Preispriorität, Limitpreisgrenzen, sichtbarer Tiefe, Participation-Cap, Teilfüllungen, Restmenge, VWAP, Spread-, Slippage- und Quote-Gebührenberechnung.
- Ergänzt die Long-/Short-Trade-Replay-Funktion des Momentum-Backtests auf zeitgestempelten Orderbuch-Snapshots. Die Auswahl von Marktdaten erfolgt ohne Look-ahead; fehlende/veraltete Exit-Daten oder Teilfüllungen lassen den Trade ausdrücklich ungelöst.
- Margin-Netto-PnL bleibt unbekannt, wenn Finanzierung nicht explizit und in der gleichen Quote-Währung belegt ist. Es werden keine USDC-Finanzierungskosten stillschweigend als USD-Nullkosten angenommen.
- Regressionstests decken Buch-Tiefe, Gebühren/VWAP, Preislimits, ungefüllte und teilweise gefüllte Aufträge, Margin-Finanzierungsnachweise und nicht abgeschlossene Exits ab.
- Orderbuch-Replay bleibt Szenario-Research; ohne qualitätsgesicherte historische L2-Daten, vollständig abgebildetes Börsenverhalten sowie OOS-/Forward-Shadow-Evidenz erfolgt weder Profitabilitätsbehauptung noch Live-Freigabe.

## 0.1.7 — CI-Blocker und Versionskonsistenz

- Fehlender `json`-Import in der positionsbezogenen Wallet-/Order-Reconciliation korrigiert, sodass Ruff nicht mehr an `F821` scheitert.
- Paketversion, Home-Assistant-Manifest und Projektmetadaten konsistent auf `0.1.7` erhöht.
- Die Benutzer-Dokumentation war noch auf `0.1.6` stehen geblieben. README-Version und Manifest sind jetzt durch den HA-Strukturvalidator und einen Regressionstest gekoppelt, damit diese Abweichung künftig die CI fehlschlagen lässt.
- Der GitHub-Actions-Lauf `38891266043` für den vorherigen Metadaten-Commit `da24444` war mit allen fünf Jobs erfolgreich. Für die nachfolgenden Änderungen ist der jeweils neue Lauf auf dem exakten Branch-Head maßgeblich.

## 0.1.6 — Datenintegrität und Steuerreport-Invalidierung

- Steuerberichte werden anhand des vollständigen kanonisierten Ereignisinhalts invalidiert; nachträgliche Korrekturen vorhandener Ereignisse (EUR-Bewertung, Gebühren, Verifikations- und Review-Status) aktualisieren bestehende Jahresreports. Der Import bleibt idempotent und insert-only; eine separate persistente Update-API erlaubt explizite spätere Steuerdaten-Anreicherung, ohne dass der nächste Rohdaten-Backfill diese Bewertung überschreibt. Historische Trades und Ledgers werden nun zusätzlich als autoritative Exchange-Datensätze aktualisiert, wenn Kraken vorhandene IDs mit korrigierten Feldern zurückliefert. Zwischen zwei Account-Snapshots werden Wallet-Balance-Deltas außerdem gegen den jüngsten Kraken-Ledger-Balancewert geprüft; fehlende Ledger-Belege, widersprüchliche gleichzeitige Ledgerstände oder unerklärte Bestandsänderungen bleiben fail-closed. Dieser Teilabgleich setzt ausdrücklich keine Gesamtportfolio-Reconciliation frei. Identische Wiederholungen bleiben idempotent; wirtschaftlich relevante Fill-Korrekturen entwerten eine zuvor verifizierte EUR-Steuerbewertung.
- OHLC-Antworten mit unbekanntem Alias werden nur akzeptiert, wenn genau ein möglicher Markt-Datensatz vorhanden ist. Mehrdeutige Antworten werden geschlossen verworfen, statt versehentlich Preise eines anderen Markts zuzuordnen.
- Regressionstests ergänzen beide Datenintegritätsfälle. Vor Positions-Exit-Auswertung und unmittelbar vor jedem Eintritt werden die betreffenden Ticker erneut von Kraken abgefragt, damit längere History-/Account-Aufrufe keine veralteten Quotes in Entscheidungen tragen. `market_max_age_seconds` wird nun tatsächlich vor Signalbewertung durchgesetzt; fehlende, zukünftige, nicht endliche oder überalterte Quote-Zeitstempel blockieren den Trade. Neue Einstiege werden für die Mengenberechnung mit dem ausführbaren Ask bei Longs beziehungsweise Bid bei Shorts bewertet, nicht mit dem letzten Handelspreis. Ticker ohne valide positive 24-Stunden-Quote-Umsatzdaten werden ausgeschlossen. Der Kraken-REST-Client erzwingt zudem ein zentrales Mindestintervall von einer Sekunde zwischen öffentlichen Spot-REST-Requests; die Prüfung ist mit einer injizierbaren Uhr ohne reale Wartezeit getestet. Im OHLCV-Szenario-Backtest ist Slippage nun nur einmal erfasst: über angepasste Ausführungspreise; Gebühren, Spread und Finanzierungsannahmen bleiben getrennte Kostenpositionen. Die ausgewiesene Brutto- und Nettorendite wird konsistent aus dem simulierten USD-PnL bezogen auf den tatsächlich angenommenen Nominalwert berechnet. Die Order-Reconciliation akzeptiert Statusänderungen erst nach Abgleich von Client-ID, Pair-Alias, tatsächlicher Orderrichtung und exakter Sollmenge; Abweichungen setzen einen Circuit Breaker. Der AddOrder-Pfad behandelt unlesbare oder strukturell unvollständige Antworten als `UNKNOWN_RECONCILING`, statt einen potenziell angenommenen Auftrag fälschlich als abgelehnt zu markieren. Die bisher ungenutzte Option `validate_shadow_orders` ist nun verdrahtet und verwendet die zentrale Order Authority mit ausschließlich Kraken-`validate=true`; sie versendet keine echte Order. Signaturkonfigurationsfehler werden dagegen noch vor dem HTTP-Aufruf als sicherer Request-Fehler klassifiziert.

## 0.1.5 — Positions-Recovery und Shadow-Exit-Planung

- Einzelne Kraken-OpenPositions-Lots werden ohne `consolidation=market` abgefragt, gespeichert und strenger auf Status, Mengen, Margin-, Bewertungs- und Zeitfelder validiert.
- Persistente Account-/Positions-Snapshots werden mit Rohantworten gespeichert; das Snapshot-Alter stammt vom tatsächlichen Exchange-Abfragezeitpunkt.
- Historienfills werden mit Order- und Ledger-Evidenz auf konkrete offene Positionen abgeglichen. Fehlende/mismatched Fill-/Order-/Ledger-Verknüpfungen erzeugen einen persistenten Circuit Breaker; der Gesamtportfolio-Abgleich bleibt ausdrücklich unvollständig.
- Teilgeschlossene Positionen werden nicht irrtümlich als vollständig abgestimmt markiert: positive `vol_closed`-Mengen benötigen eine lotgenaue Zuordnung der schließenden Fills; andernfalls entsteht ein persistenter Circuit Breaker.
- Eine pro Position getrennte Shadow-Exit-Policy protokolliert +3,5-%-Teilgewinn auf 50 % der verbleibenden Menge und −3-%-Stop zur Risikoreduktion. Ausführung, echte Netto-PnL, Finanzierung und börsenseitiger Stop sind nicht freigegeben.
- Positionsmanagement-Einstellungen sind begrenzt und über HA-Schema/Konfiguration getestet.
- Backtest-Rechnung korrigiert: Margin-Kosten werden nicht durch die Hebelquote künstlich reduziert; Trade-PnL basiert auf Brutto-PnL minus Kosten, dynamischer Kontogröße und tatsächlichem Notional. Ergänzt wurden Sortino-/Tail-Loss-/Recovery-/Turnover-/Kostenanteil- und Long-/Short-Kennzahlen sowie Eingabevalidierung.
- Settlement-Intents verlangen passende Position-, Währungs-, Mengen-, Snapshot- und Ledger-Evidenz.
- Margin-Kosten, Reconciliation und kalibrierter Netto-Edge bleiben ausdrückliche Freigabebedingungen.
- Der unveränderliche `LIVE_EXECUTION_RELEASED=False`-Schalter wurde entfernt. `OrderAuthority` verwendet ausschließlich die sichtbaren LIVE-/Kill-Switch-Konfigurationen für die Ausführungsberechtigung; Runtime-Intents laufen bei aktiviertem LIVE-Modus durch die zentrale Order Authority, nachdem alle vorherigen Risk Gates bestehen.

## 0.1.4 — Margin-Gebührenbasis und Fail-Closed-Korrekturen

- Margin-Eröffnungs- und Rollover-Kosten werden konservativ auf den vollen erweiterten Positionswert bezogen; die Hebelstufe reduziert die Gebührenbasis nicht künstlich.
- Eine bekannte Eröffnungsgebühr wird auch bei Haltedauern unter vier Stunden vollständig berücksichtigt, selbst wenn noch kein Rollover-Satz erforderlich ist.
- Nicht-endliche Margin-Gebührensätze und ungültige Haltedauern werden mit explizitem Ablehnungsgrund geschlossen abgewiesen.
- Regressionstests decken Gebührenbasis, kurze Haltedauer, NaN-Raten und NaN-Haltedauer ab.
- Echte Margin-Eröffnung bleibt bis zur bestätigten konto-/positionsbezogenen Rate, vollständigen Reconciliation und weiteren Freigabekriterien gesperrt.

- ClosedOrders-Historie wird über den Kraken-Adapter abgefragt und getrennt im Kontosnapshot auditiert; automatische Reconciliation zwischen Orders, Fills, Positionen und Ledger bleibt offen.
- Tests für ClosedOrders-Paginierung, Filtervalidierung und die zusätzlichen Snapshot-Daten ergänzt.
- Shadow-Entscheidungen werden beim Insert atomar als terminaler Zustand `SHADOW_ONLY` persistiert und nicht mehr als ungelöste echte Order-Intents aufgelistet.
- Regressionstests verhindern, dass Shadow-Intents in einen Submit-Zustand überführt werden oder durch einen unbekannten Initialzustand angelegt werden.
- Das unabhängige Risikogate akzeptiert keine positive, aber unkalibrierte Edge-Prognose; die derzeitige Momentum-Projektion bleibt ausdrücklich Research-only.
- Shadow-Entscheidungen werden dadurch bei fehlender OOS-/Forward-Validierung als `EDGE_NOT_VALIDATED` blockiert statt zur Orderfreigabe aufzusteigen.
- Beobachtete offene und geschlossene Exchange-Orders werden anhand der Kraken-Order-ID mit Status, Client-ID, Menge, Kostenfeldern und Rohantwort persistent gespeichert und bei Folgeabfrage aktualisiert; dies ersetzt noch keine vollständige Order-/Fill-/Ledger-Reconciliation.
- Malformed Order-Snapshot-Strukturen aktivieren einen persistenten Circuit Breaker und blockieren Risikoerhöhungen.
- Eindeutige Ordermatches werden fail-closed gegen Status und ausgeführte Menge abgeglichen; fehlende abgesendete Orders, mehrdeutige Matches, Mengenwidersprüche und unbekannte offene Orders lösen einen Circuit Breaker aus. Der Mechanismus setzt die Gesamtportfolio-Reconciliation nicht auf erfolgreich.
- TradesHistory und ClosedOrders wurden auf die offizielle Kraken-Cursor-Paginierung (`with_cursor`/`cursor.next`) umgestellt; opake Cursor werden persistent gespeichert und nach Neustart fortgesetzt. Cursor-Snapshots verlassen sich nicht auf den in Cursor-Modus nicht aussagekräftigen ClosedOrders-Count. Ledgers bleiben offsetbasiert; unvollständige Backfills werden nicht als vollständig ausgewiesen.
- Trades-History-Fills werden anhand des Kraken-`margin`-Felds technisch zwischen Margin und Cash Spot getrennt. Fehlende oder ungültige Produktdaten lassen den History-Backfill fehlschlagen; verwandte Ledger-IDs bleiben im Roh-Payload erhalten.
- Account- und Positions-Snapshots werden dauerhaft und idempotent mit Rohantwort, Balance, TradeBalance, offenen/geschlossenen Orders sowie normalisierten Marginpositionsfeldern persistiert; beschädigte Positionsstrukturen setzen keine Vollständigkeitsbehauptung.
- OpenPositions wird nicht nach Markt konsolidiert, damit mehrere Margin-Lots je Paar einzeln nachverfolgbar bleiben; Settlement-Intents verlangen überprüfbare Position-/Kredit-/Ledger-Evidenz und verwenden keine unbestätigte Nullmengen-Abkürzung.

## 0.1.3 — Mehrjährige Steuerexporte und Versionskonsistenz

- Steuerberichte werden für alle in der persistenten Historie vorhandenen Jahre plus das aktuelle österreichische Steuerjahr generiert.
- Die Jahresbestimmung verwendet Europe/Vienna statt UTC, damit Ereignisse und Berichtsjahre dieselbe österreichische Kalenderlogik nutzen.
- Regressionstest für historische Jahre, Berichtscache-Invalidierung und unvollständige Quellabdeckung ergänzt.
- Kraken User-Agent, HA-Manifest, Python-Paket und Projektmetadaten verwenden konsistent Version 0.1.3.
- Nicht rekonstruierte Tages-PnL- und Drawdown-Baselines bleiben unbekannt und blockieren neue Risikoerhöhungen; ein stillschweigender Nullwert entfällt.


## 0.1.2 — Historienimport und HA-Versionkonsistenz

- Kraken Trades History und Ledgers werden mit persistierten Seiten-Cursorn schrittweise importiert und idempotent gespeichert.
- Festes Zeitfenster je Sync-Generation, Cursor-Fortsetzung für TradesHistory/ClosedOrders, Offset-Fortsetzung für Ledgers und überlappendes inkrementelles Aktualisierungsfenster.
- Fehlerhafte Daten, fehlende Counts, unerwartete leere Seiten und veränderte Counts halten den Import explizit unvollständig.
- Die Steuerbelegdateien werden nach dem Import aktualisiert; fehlende EUR-Bewertungen und vollständige Quellreconciliation bleiben als offene Punkte markiert.
- HA-Manifest, Python-Paketversion und `pyproject.toml` werden im Validator dynamisch auf Übereinstimmung geprüft.

## 0.1.1 — Risikopfade und Datenqualität

- Nettoexposure berücksichtigt die Positionsrichtung; Bruttoexposure verwendet den absoluten Positionswert.
- Ungültige numerische Konto-, Positions- und Orderdaten werden geschlossen abgewiesen.
- USD-Krypto-Universum akzeptiert nur explizite reguläre Währungs-Assetklassen; unbekannte Klassen werden ausgeschlossen.
- Ordermengen werden auf Lot-Präzision abgerundet; Mindestmengen werden danach erneut geprüft.
- Kostenmodell wählt Margin nur bei vollständigen Kostendaten und positivem Netto-Edge; nur Long-Signale dürfen auf Cash-Spot zurückfallen.
- Margin-Short-Schließungen können nicht als normale Spot-Kauforders erzeugt werden.
- Persistenter, paginierter, idempotenter TradesHistory-/Ledgers-Import mit festen Zeitfenstern und Fehler-/Count-Prüfung ergänzt.
- Steuerreport wird aus importierten Events aktualisiert; EUR-Werte und Quellabdeckung bleiben ohne vollständige Reconciliation ausdrücklich unbestätigt.
- Quellabdeckung der Steuerreports bleibt ausdrücklich unvollständig; keine Live-Freigabe.


## 0.1.0 — initiale eigenständige Basis

- Neues Home-Assistant-Add-on-Grundgerüst, getrennt von Kraken-Trade-Real.
- Fail-closed-Konfiguration mit internen Obergrenzen für Positionsrisiko, Exposure, Margin und Hebel.
- Kraken Spot REST-Client, persistente Nonce, dynamische USD-Krypto-Marktauswahl und Trend-Signale.
- SQLite-Persistenz für Ereignisse, Bars, Order-Intents, Fills, Ledger- und Steuerereignisse sowie Circuit Breaker.
- Separate Order Authority mit Order-State-Machine und internem Live-Release-Gate.
- Tests und GitHub Actions für Qualität, Sicherheit, HA-Struktur, Compile und Container-Build.

### Noch nicht freigegeben

- Kein echter Orderbetrieb; die Live-Freigabe bleibt im Quellcode deaktiviert.
- Kein nachgewiesener vollständiger Konten-/Ledger-Abgleich oder vollständiges PnL.
- Kein vollständiger Orderbuch-Ausführungssimulator, keine unangetastete Out-of-Sample-Validierung und kein abgeschlossener Forward-Shadow-Test.
- Keine verifizierte Konto-spezifische USDC-/Margin-Gebührenzuordnung.
- Steuerexporte sind unterstützende Datenaufbereitung; nicht amtliches Steuerreporting und keine Steuerberatung.