# Changelog

## 0.1.6 — Datenintegrität und Steuerreport-Invalidierung

- Steuerberichte werden anhand des vollständigen kanonisierten Ereignisinhalts invalidiert; nachträgliche Korrekturen vorhandener Ereignisse (EUR-Bewertung, Gebühren, Verifikations- und Review-Status) aktualisieren bestehende Jahresreports. Der Import bleibt idempotent und insert-only; eine separate persistente Update-API erlaubt explizite spätere Steuerdaten-Anreicherung, ohne dass der nächste Rohdaten-Backfill diese Bewertung überschreibt. Historische Trades und Ledgers werden nun zusätzlich als autoritative Exchange-Datensätze aktualisiert, wenn Kraken vorhandene IDs mit korrigierten Feldern zurückliefert. Identische Wiederholungen bleiben idempotent; wirtschaftlich relevante Fill-Korrekturen entwerten eine zuvor verifizierte EUR-Steuerbewertung.
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
- Margin-Kosten, Reconciliation und kalibrierter Netto-Edge bleiben Freigabebedingungen; `LIVE_EXECUTION_RELEASED=False` bleibt unverändert.

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