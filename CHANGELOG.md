# Changelog

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

## 0.1.3 — Mehrjährige Steuerexporte und Versionskonsistenz

- Steuerberichte werden für alle in der persistenten Historie vorhandenen Jahre plus das aktuelle österreichische Steuerjahr generiert.
- Die Jahresbestimmung verwendet Europe/Vienna statt UTC, damit Ereignisse und Berichtsjahre dieselbe österreichische Kalenderlogik nutzen.
- Regressionstest für historische Jahre, Berichtscache-Invalidierung und unvollständige Quellabdeckung ergänzt.
- Kraken User-Agent, HA-Manifest, Python-Paket und Projektmetadaten verwenden konsistent Version 0.1.3.
- Nicht rekonstruierte Tages-PnL- und Drawdown-Baselines bleiben unbekannt und blockieren neue Risikoerhöhungen; ein stillschweigender Nullwert entfällt.


## 0.1.2 — Historienimport und HA-Versionkonsistenz

- Kraken Trades History und Ledgers werden mit persistierten Seiten-Cursorn schrittweise importiert und idempotent gespeichert.
- Festes Zeitfenster je Sync-Generation, Offset-Fortsetzung und überlappendes inkrementelles Aktualisierungsfenster.
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