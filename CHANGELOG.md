# Changelog

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