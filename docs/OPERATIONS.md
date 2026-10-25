## Version 0.1.47 — Offline-Research für Strategievergleiche

Der Research-Pfad ist ein Entwickler-/Analysewerkzeug. Er wird nicht vom Runtime-Zyklus aufgerufen, schreibt nicht in die Produktiv-SQLite-Datenbank und kann keine Order erzeugen. Er soll den negativen bisherigen TSM-Befund mit vorab festgelegten Alternativen prüfen, nicht den aktuellen Trade-Gate übersteuern.

### Datenimport und Vergleich

Kraken veröffentlicht historische OHLCVT-Dateien als headerlose CSV mit sieben Spalten: timestamp, open, high, low, close, volume, trades. Der Parser validiert Zeilen und meldet Quellen-SHA-256, identische Duplikate sowie zeitliche Lücken. Konfligierende Duplikate, ungeordnete Daten, nicht endliche Werte und ungültige OHLC-Verhältnisse führen zu einem Fehler. Lücken werden nie durch künstliche Kerzen ersetzt.

Die erste Vergleichsstufe akzeptiert ausschließlich 60-Minuten-Daten und vergleicht drei vorab festgelegte Kandidaten: aktuelle TSM-Regel, 24-Bar-Mean-Reversion und eine einfache L2-logistische Regression. Cash und Buy-and-Hold werden getrennt als Referenzen ausgewiesen. Die ML-Standardisierung und das Modell werden nur mit dem 60-Prozent-Trainingsabschnitt geschätzt. Ein chronologischer 20-Prozent-Validierungs- und 20-Prozent-Testabschnitt bleiben außerhalb des Fits; Zielhorizonte, die eine Abschnittsgrenze überschreiten würden, werden ausgeschlossen. Dies ist eine feste chronologische Holdout-Auswertung, noch keine expanding-window Walk-Forward-Rekalibrierung.

Gebühren, angenommener Round-trip-Spread und Slippage müssen übergeben werden. Die Margin-Kostenszenarien sind Sensitivitätsbudgets, keine Kraken-Quotes. Ergebnisse verwenden den nächsten Kerzen-Open als Einstieg und den Open nach dem festen Haltehorizont als Ausgang; je Kandidat werden zeitlich nicht überlappende Trades ausgewertet. Das ist kein vollständiger Ausführungssimulator: Hebelwirkung, Equity-Positionsgrößen, Orderbuch, partielle Fills, echte historische Fees, tatsächliche Margin-Kosten, Liquidation, Cross-Pair-Korrelation und Portfolio-PnL werden nicht modelliert.

Zur Anzeige der verfügbaren Kommandozeilenoptionen:

```bash
python scripts/compare_strategies.py --help
```

Die Vergleichs-CLI benötigt konkrete Kosteneingaben und schreibt optional einen JSON-Bericht in einen vom Aufrufer gewählten Pfad. Die Sensitivitätsbudgets 5, 10, 20 und 40 bps sind ausschließlich Annahmen und bestätigen keine Gebühren. Das Programm meldet release_gate_eligible=false unabhängig vom Ergebnis. Kein automatisches Kandidaten-Ranking, Strategie-Wechsel oder Parameter-Promotion ist implementiert.

Offizielle Datenbeschreibung: https://support.kraken.com/de/articles/360047124832-downloadable-historical-ohlcvt-open-high-low-close-volume-trades-data

## Version 0.1.46 — Überlappungsbereinigte Forward-Diagnostik

Die `SHADOW_RESEARCH_SUMMARY`-Ausgabe enthält zusätzlich `non_overlapping_costed_forward_diagnostics`. Die Diagnostik beginnt nur mit abgeschlossenen, kostenbewerteten Forward-Beobachtungen, gruppiert nach Handelspaar und Entscheidungszeit und übernimmt deterministisch die früheste Beobachtung, deren Entscheidungszeit nicht vor dem Zielzeitpunkt der zuletzt übernommenen Beobachtung liegt. Überlappende Beobachtungen desselben Paars werden gezählt und ausgelassen; Zeitangaben ohne plausiblen Zielhorizont werden separat gezählt.

Der Bericht enthält die resultierende Stichprobengröße, Ausschlusszahlen, Netto-Kosten-Proxys insgesamt und getrennt nach LONG/SHORT sowie chronologische 60/20/20-Teilgruppen. Diese Kennzahlen ergänzen die bestehende, weiterhin überlappende Hauptreihe; sie überschreiben keine gespeicherten Ergebnisse. Zeitlich entkoppelte Labels verschiedener Paare können weiterhin korreliert sein. Die Kennzahl ist daher weder ein Satz unabhängiger Trades noch Portfolio-PnL, keine belastbare Margin-Netto-Rendite und kein Live-Freigabegate. `release_gate_eligible` bleibt `false`.

## Version 0.1.44 — Kompakte Konsole und vollständiges Audit

Home Assistant `stdout` enthält weiterhin eine JSON-Zeile pro sichtbarem Ereignis. Wiederkehrende Markt-Signale und `NO_TRADE`-Entscheidungen zeigen die entscheidenden Felder, wobei die Console-Rundung explizit markiert ist. Historien- und Berechtigungsstatus werden auf den operativen Status reduziert, solange keine Lücken, Fehler oder fehlenden Rechte vorliegen. Fehler, unvollständige Daten, Sperren, Positions-/Orderereignisse, Abweichungen und Zyklusabschlüsse bleiben vollständig sichtbar.

`SHADOW_RESEARCH_SUMMARY`, `WALLET_BALANCE_RECONCILIATION_SUMMARY` und `OHLCV_BACKTEST_SCENARIO_REPORT` werden bei erstmaligem Auftreten oder Inhaltsänderung vollständig ausgegeben. Identische Wiederholungen erhalten eine kompakte Meldung mit den wichtigsten Kennzahlen und `report_fingerprint`. Ein Wallet-Mismatch oder relevante Evidence-Gaps werden nie unterdrückt. Fingerprints ignorieren nur zyklus-/beobachtungsbezogene IDs, nicht die Ergebnisdaten.

**Die Datei `/data/logs/events.jsonl` bleibt die vollständige Auditquelle.** Jeder redigierte Event-Datensatz wird dort einschließlich genauer Zahlen, Detailfelder, Array-Einträge und wiederholter Ereignisse gespeichert. Das Console-Format ist eine reine Darstellungsschicht; Rückgabewerte von `AuditLogger.emit`, Strategie, Risiko- und Orderlogik sowie Datenbankverhalten ändern sich nicht. Zahlen in der Console sind ausdrücklich gerundete Anzeigeformate; für exakte Werte ist der Auditdatensatz maßgeblich.

## Version 0.1.43 — Zeitlich korrekter OHLCV-Exit und Teilgewinn-Szenario

CV-Exit und Teilgewinn-Szenario

Die OHLCV_BACKTEST_SCENARIO_REPORT-Ausgaben verwenden jetzt den Exit-Zeitpunkt konsequent: Wird eine Haltedauer von acht Stunden ausgewertet, schließt die Simulation am Open des geplanten Exit-Balkens. Dessen spätere High-/Low-Werte können keinen früheren Stop mehr auslösen. Stop und Teilgewinnmitnahme werden in vorhergehenden Balken ausgewertet; wenn Stop und Ziel in derselben Stundenkerze liegen, gilt Stop-zuerst als konservative Annahme. Ein Take-Profit wird einmalig auf den konfigurierten Anteil angewandt; die Restposition bleibt bis Stop oder Time-Exit offen. Reports nennen exit_model_version, intrabar_ambiguity_policy, time_exit_policy, partial_take_profit_policy und Exit-Gründe. Diese Kennzahlen sind Forschungsszenarien, keine realen Fills und keine Freigabe echter Orders.


## Version 0.1.42 — Ergänzende Zielhorizont-Messung

SHADOW_RESEARCH_SUMMARY enthält zusätzlich target_open_gross_proxy_all, target_open_gross_proxy_by_direction, target_open_gross_chronological_splits und target_open_timing_diagnostics. Diese Reihe misst nur die Brutto-Preisbewegung zum Open der Stundenkerze, deren Intervall den Zielzeitpunkt strikt enthält. Sie steht separat neben der bisherigen Schlusskurs-/Spot-Kostenproxy-Reihe, überschreibt diese nicht und ist weder ein tatsächlicher Fill noch Portfolio-PnL. Ältere abgeschlossene Beobachtungen werden anhand lokal gespeicherter Kerzen ergänzt; ein SHADOW_RESEARCH_TARGET_OPEN_BACKFILL-Eintrag entsteht nur, wenn Ergänzungen vorgenommen wurden. Keine Research-Kennzahl kann Live-Ausführung freigeben.


## Version 0.1.41 — Entry-Idempotenz und Forward-Diagnostik

Die Signalentscheidung für einen Einstieg hat nun einen deterministischen Schlüssel aus Strategieversion, Kraken-Paar-ID, Zeitstempel der letzten geschlossenen Stundenkerze und Richtung. Derselbe Kandidat wird damit nicht bei jedem 5-Minuten-Lauf als neue Handelsentscheidung behandelt. Bereits persistierte Entscheidungen werden im AppLog als `ENTRY_THESIS_ALREADY_ATTEMPTED` ausgegeben und nicht erneut versucht. Nach einem neuen Kerzenschluss entsteht eine neue Entscheidung; eine terminal abgelehnte oder unbekannt ausgegangene frühere Entscheidung wird innerhalb derselben Kerze nicht automatisch wiederholt.

Bevor eine neue Risikoerhöhung zugelassen wird, prüft `ENTRY_BLOCKED_EXISTING_PAIR_EXPOSURE`, ob für dasselbe Paar bereits eine offene Position oder Order existiert. Falls die Paar-ID in einer offenen Position/Order nicht zuverlässig identifiziert werden kann, blockiert der Guard ebenfalls. Die Version unterstützt dadurch kein implizites Aufstocken und kein automatisches Umkehren einer bestehenden Position. Diese Aktionen benötigen einen eigenen, positionsbewussten Ablauf.

`LATEST_CLOSED_BAR_STALE_OR_INVALID` blockiert Richtungssignale, deren Kerzenschluss älter als der konfigurierte Maximalabstand ist oder einen ungültigen/future Zeitstempel hat. Vorher wurde der Bar-Zeitpunkt nur für die Research-Beobachtung geprüft, nicht verbindlich als neues Entry-Gate.

Die Research-Zusammenfassung trennt nun Brutto-Forward-Bewegungen von geschätzten kostenbereinigten Spot-Ergebnissen und zeigt die Abweichung des tatsächlichen bewerteten Stundenkerzenschlusses vom Zielhorizont. Die Labels berücksichtigen keine tatsächlichen Fills, Intrabar-Stops, Take-Profits oder Teilverkäufe und bleiben daher reine Marktbewegungs-Labels.

## Version 0.1.40 — Trendqualität und Kosten-Diagnostik

`MARKET_SIGNAL_EVALUATED` enthält jetzt `trend_efficiency_24h`, `trend_efficiency_72h` und `trend_strength_score`. Ein Signal muss weiterhin das übereinstimmende 24-/72-Stunden-Momentum und die Mittelwertbestätigung erfüllen. Zusätzlich verwirft der neue Filter Trendkandidaten, deren Netto-Bewegung im Verhältnis zum gesamten absoluten Preisweg zu gering ist. Die Startwerte 0,20 (24 Stunden) und 0,12 (72 Stunden) sind vorab gesetzte Forschungsfilter, nicht angepasste oder ergebnisgarantierende Parameter.

Der Score ist eine begrenzte Diagnose der Trendstärke, keine Eintrittswahrscheinlichkeit und kein erwarteter Netto-Return. Die bisherige lineare Momentum-Projektion wird in Entry-Logs explizit als `UNVALIDATED_LINEAR_MOMENTUM_HEURISTIC` und `gross_edge_proxy_is_expected_return=false` markiert. `NO_TRADE` nennt, ob diese Projektion unter der geschätzten Spot-Kostenschwelle liegt oder Margin-Kostendaten fehlen. Der Risk-Gate verlangt weiterhin eine separat validierte Edge.

`cost_diagnostic` unterscheidet bekannte Handelsgebühren, Spread und angenommene Slippage von unbekannten Margin-Eröffnungs-/Rollover-Gebühren. Ein unvollständiges Margin-Kostenmodell erhält keine Gesamtkosten als Zahl; bekannte Handelsgebühren werden trotzdem ausgewiesen.

`WALLET_BALANCES_STABLE_NO_LEDGER_ACTIVITY` bedeutet nur, dass sich die beobachteten Wallet-Salden im abgegrenzten Snapshot-Zeitraum nicht verändert haben und keine passende Ledger-Aktivität festgestellt wurde. Es bedeutet **nicht** Ledger-Verifikation. `WALLET_BALANCES_MATCH_LEDGER_WINDOW` wird nur vergeben, wenn mindestens ein Asset über einen passenden Ledger-Bestand im Delta-Zeitraum geprüft wurde und kein Widerspruch vorliegt. Beide Statuswerte sind nur ein begrenzter Wallet-Delta-Abgleich, keine vollständige Portfolio-Reconciliation.

Die neuen Beobachtungen speichern zusätzlich beide Effizienzmerkmale direkt in SQLite (Schema 7); bestehende Daten werden durch eine additive Migration erhalten und ältere Zeilen erhalten für diese neuen Features `NULL` (unbekannt); sie werden nicht nachträglich als Effizienz `0` fehlinterpretiert. Research-Zusammenfassungen filtern explizit nach der aktiven Strategieversion, damit `tsm-0.1.2`-Ergebnisse nicht mit `tsm-0.2.0` zusammengezählt werden.

Die Strategieversion `tsm-0.2.0` beginnt eine neue Forschungsserie; vorherige `tsm-0.1.2`-Beobachtungen werden nicht als Ergebnisse der neuen Regel behandelt. Kein neuer Bericht kann automatisch Parameter promoten oder die interne Live-Sperre verändern.

## Version 0.1.39 — Persistente Shadow-Research-Pipeline

Der Runtime-Zyklus speichert pro Paar, geschlossenem Stundenbalken, Strategieversion und festgelegten Momentum-Schwellen genau eine Research-Beobachtung. LONG-, SHORT- und gültige NONE-Signale werden erfasst; unzureichende Historie und veraltete Kerzen werden nicht als valide Beobachtungen bewertet. Die Datenbankmigration aktualisiert das Schema auf Version 7, ohne bestehende Orders, Historie oder Kontosnapshots zu löschen.

Zusätzlich wird bei einer neuen Beobachtung und vorhandener Taker-Gebühr ein `OHLCV_BACKTEST_SCENARIO_REPORT` ausgegeben, sofern das Instrument beide Margin-Richtungen unterstützt. Die Historie wird nur bei mindestens 500 lückenlosen Stundenkerzen ausgewertet. Train, Validation und Test werden chronologisch getrennt. Es gibt vier separate Budgets für die kombinierten Margin-Eröffnungs-/Rollover-Kosten: 5, 10, 20 und 40 Basispunkte pro simuliertem Roundtrip. Diese Budgets sind explizite Sensitivitätsannahmen, keine aktuellen Kraken-Gebührenangebote. Der Bericht nutzt den aktuellen Spread und die aktuelle Taker-Stufe nur als historische Proxys sowie 2x Hebel und 10 Prozent Positionsgröße; er ist ausdrücklich `SCENARIO_ONLY_NOT_LIVE_VALIDATION` und steuert keine Entries.

Relevante AppLog-Ereignisse:

- `SHADOW_RESEARCH_OBSERVATION_RECORDED`: Signal, Kerzenzeit, Entscheidungszeit, Einstieg-Midpreis, aktueller Spread, verfügbare Kraken-Taker-Gebühr und Zielzeit.
- `SHADOW_RESEARCH_OUTCOME_SETTLED`: Forward-Kursbewegung am Zielhorizont, geschätzte Spotkosten und Break-even-Budget für unbekannte Margin-Gebühren.
- `SHADOW_RESEARCH_OUTCOME_INCOMPLETE`: Zielkerze fehlt; kein Kursresultat wird erfunden.
- `SHADOW_RESEARCH_SUMMARY`: Gesamtstichprobe, Long-/Short-Aufteilung, kostenbereinigte Proxy-Kennzahlen, chronologische Teilmengen und Einschränkungen.

Die Kostenformel nutzt bei bekannter `TradeVolume`-Taker-Gebühr zweimal die aktuelle Gebühr, den halben Entry-/Exit-Spread im Mittel und 5 Basispunkte angenommene Slippage pro Seite. Der Exit-Spread ist beim Eintreffen der Zielkerze aktuell beobachtet, aber kein historischer Orderbuch-Spread. Das ist daher ein Shadow-Signaltest, kein realer Fill- oder Portfolio-PnL-Nachweis.

Margin-Eröffnungs- und Rollover-Gebühren sind in dieser Stufe weiterhin unbekannt. `margin_cost_break_even_bps` ist nur die höchstens tragbare kombinierte Margin-Gebühr, bevor die geschätzte Nettobewegung null erreicht; es ist keine Gebührenquote von Kraken. Der Report kennzeichnet diese Lücke und verändert niemals `edge_calibrated` oder die interne Live-Freigabe. Die letzte zeitliche Teilmenge bleibt Holdout; auch bei ausreichender Stichprobe ist der Status nur `HOLDOUT_AVAILABLE_REVIEW_REQUIRED` und verlangt weitere manuelle Prüfung.

## Version 0.1.38 — Kontrollierte API-Fehler-Erholung

Nur der exakte Fehler `KRAKEN_API:KRAKEN_PUBLIC_NETWORK_ERROR` kann nach einem erfolgreichen vollständigen Lesezyklus automatisch aufgelöst werden. Voraussetzungen sind Kraken-Status `online`, nichtleere Markt- und Tickerdaten vor und nach den Kontenabfragen, erfolgreiche private Leserechte, vollständige Trade-/Ledger-/ClosedOrders-Historie ohne Evidenzlücken und ein verfügbarer Kontosnapshot.

Die Auflösung markiert den Datenbankeintrag inaktiv, löscht ihn nicht und protokolliert `CIRCUIT_BREAKER_RECOVERED`. Andere Circuit Breaker bleiben unverändert. Portfolioabgleich, Kostenmodell, Edge-Kalibrierung und interne Live-Freigabe bleiben separate Gates; diese Änderung schaltet Live-Handel nicht frei.

## Version 0.1.37 — Ursachen persistenter Circuit Breaker nachvollziehbar machen

Das Ereignis `CIRCUIT_BREAKER_STATUS` wird je regulär abgeschlossenen Zyklus einmal geschrieben und enthält `active`, `count`, `new_entries_blocked` sowie eine begrenzte Liste aktiver Sperren mit Name, Grund, Zeitstempel und ausgewähltem Diagnosekontext. Bei Wallet-Abstimmungen werden abweichende Assets gezielt genannt. Eine Sperre wird nicht automatisch zurückgesetzt oder verändert.

`NO_TRADE` mit Grund `PERSISTED_CIRCUIT_BREAKER_ACTIVE` enthält jetzt die zugehörigen Diagnoseinformationen. `CYCLE_SUMMARY` gibt `active_circuit_breaker_count` und `active_circuit_breakers` aus. Damit lässt sich die Ursache einer bestehenden Sperre im AppLog bestimmen, ohne die persistente Schutzmaßnahme zu umgehen.

Diese Version ist weiterhin nicht für unbeaufsichtigten Live-Handel freigegeben.

## Version 0.1.36 — technische Sperre für Live-Orders

`INTERNAL_LIVE_EXECUTION_RELEASED` ist eine interne, nicht über Home-Assistant-Optionen überschreibbare Konstante. Sie bleibt in dieser Version `False` und wird von der zentralen `OrderAuthority` vor jedem echten Orderaufruf geprüft. Ein falsch oder versehentlich gesetzter Live-Schalter kann diese Sperre nicht umgehen. `validate=true` für reine API-Validierung ist davon getrennt und löst keine echte Ausführung aus.

Nach der Historien-Reconciliation protokolliert `KRAKEN_HISTORY_RECONCILED` die aktualisierten Qualitätslücken sowie `history_ready_for_entries`. `CYCLE_SUMMARY` enthält zusätzlich `internal_live_release`, `history_ready_for_entries` und `history_evidence_gaps`. Die frühere `KRAKEN_HISTORY_SYNC`-Meldung kann weiterhin den Stand vor dem gezielten Abgleich zeigen; für den finalen Status ist daher die Reconciled-Meldung maßgeblich.

Diese Version erteilt keine Live-Trading-Freigabe. Ein künftiger Release darf den internen Wert erst ändern, wenn sämtliche verpflichtenden Konto-/Portfolio-, Margin-, Kosten- und Strategie-Abnahmekriterien nachgewiesen sind.

## Version 0.1.35 — Trades-Export für fehlende Ledger-Verknüpfungen

Wenn QueryTrades und der gezielte TradesHistory-Fallback keine Ledger-IDs liefern, verwendet die App den dokumentierten Kraken-Trades-Export. Er wird auf ein Fenster von 15 Sekunden vor und nach dem gespeicherten Börsenzeitpunkt begrenzt. Die App fragt AddExport an, speichert die Export-ID direkt in der Trade-Historie und prüft ExportStatus in späteren Zyklen. Nach Status `Processed` lädt RetrieveExport die ZIP-Datei; die CSV wird im Arbeitsspeicher gelesen und nicht als dauerhafte Datei gespeichert.

Eine Zeile wird nur verarbeitet, wenn ihre eindeutige `txid` dem gespeicherten Trade entspricht. Order-ID, Zeit, Richtung, Preis, Volumen und Gebühr werden zusätzlich abgeglichen. Doppelte Treffer, Abweichungen, fehlende Felder und nicht verfügbare Exporte heben die Sperre nicht auf. Nach erfolgreicher Wiederherstellung mit Ledger-IDs wird der fertige Export mit RemoveExport gelöscht. Für diesen Recovery-Schritt ist die Kraken-Berechtigung `export-data` erforderlich; die im aktuellen Benutzer-Log erkannte Berechtigung ist bereits vorhanden.

Im Ereignis `KRAKEN_TRADE_LEDGER_RECONCILIATION` zeigen `trade_rows_export_requested`, `trade_rows_export_status_checked`, `trade_rows_export_recovered`, `trade_rows_export_empty_ledgers`, `trade_rows_export_pending`, `trade_rows_export_deferred`, `trade_rows_export_failures`, `trade_rows_export_status_missing` und `next_trade_export_retry_epoch` den Stand an. Leere Ledger-Listen werden erst nach unabhängig vollständigem Ledger-/refid-Abgleich bestätigt. Keine Änderung an der Live-Freigabe.

Offizielle Referenz: [Request Export Report](https://docs.kraken.com/api-reference/account-data/request-export), [Get Export Report Status](https://docs.kraken.com/api-reference/account-data/get-export-report-status), [Retrieve Data Export](https://docs.kraken.com/api-reference/account-data/retrieve-data-export), [Delete Export Report](https://docs.kraken.com/api-reference/account-data/delete-export-report) und [Trades-Historie: Felder und Ledger-Referenzen](https://support.kraken.com/de/articles/360001184886-how-to-interpret-trades-history-fields?mode=consumerapp).

## Version 0.1.34 — gezielter Trade-/Ledger-Fallback


## Version 0.1.32 – bestehende HA-Mounts direkt verwenden

Version 0.1.31 hat nach den vorherigen Änderungen noch versucht, `/data/state/private` und `/data/logs/private` anzulegen. Auf den bestehenden, bereits auf UID/GID `10001:10001` und Modus `0700` gesetzten Mountverzeichnissen war das für den Root-Prozess nicht erlaubt. Version 0.1.32 legt deshalb keine Unterverzeichnisse unter `/data/state` und `/data/logs` an; der Trader nutzt diese Verzeichnisse direkt. Ihre Modi werden nicht geändert. Für den Migrationsschritt wird ihre Eigentümerschaft vorübergehend an root und vor dem Benutzerwechsel zurück an UID/GID `10001:10001` gegeben; Dateien auf der obersten Verzeichnisebene werden einzeln neu zugeordnet, ohne rekursiv in alte Unterverzeichnisse abzusteigen. Die Datenbank bleibt `/data/state/kraken_trader.sqlite3`, das AppLog `/data/logs/events.jsonl`. Aktualisiere auf Version `0.1.32` und prüfe im Startprotokoll `version=0.1.39` sowie `uid=10001 gid=10001`.
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
3. `Kraken Margin Trader` installieren bzw. über **Aktualisieren** auf Version `0.1.39` aktualisieren. Nach der Aktualisierung muss das Startprotokoll `version=0.1.32` und die Python-Bootstrap-Diagnose `uid=10001 gid=10001` zeigen; andernfalls läuft noch nicht das neue Image oder der Privilegienwechsel ist fehlgeschlagen. Anschließend starten und das App-Protokoll prüfen.
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
