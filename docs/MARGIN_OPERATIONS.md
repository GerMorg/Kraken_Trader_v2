# Kraken Spot Margin, EWR-Finanzierung und Settlement

## Offizielle API-Referenz

- Add Order: https://docs.kraken.com/api-reference/trading/add-order
- Open Positions: https://docs.kraken.com/api-reference/account-data/get-open-positions
- Trade Balance: https://docs.kraken.com/api-reference/account-data/get-trade-balance
- Open Orders: https://docs.kraken.com/api-reference/account-data/get-open-orders
- Closed Orders: https://docs.kraken.com/api-reference/account-data/get-closed-orders
- Trade History: https://docs.kraken.com/api-reference/account-data/get-trades-history
- Ledgers: https://docs.kraken.com/api-reference/account-data/query-ledgers
- EWR-Konvertierungen: https://support.kraken.com/articles/margin-conversion-eea
- Margin-Gebühren: https://support.kraken.com/at/articles/206161568-what-are-the-fees-opening-and-rollover-for-trading-using-margin-
- Margin-Settlement: https://support.kraken.com/at/articles/218741078-settling-a-spot-position-on-margin
- Margin-Berechtigung: https://support.kraken.com/at/articles/4402532394260-client-eligibility-for-margin-trading-services-

Diese Dokumentation muss bei jeder zukünftigen Freigabe erneut geprüft werden; einzelne Felder und Kontoberechtigungen können sich ändern.

## Markt- und API-Verhalten

Die App verwendet den aktuellen AssetPairs-Bestand, nicht fest programmierte Hebelstufen. Es werden nur online Krypto-Spot-Märkte mit USD-Quote aufgenommen. Tokenisierte Aktien und nicht USD-notierte Märkte sind ausgeschlossen. `leverage_buy` und `leverage_sell` werden separat betrachtet; die gewählte Stufe muss tatsächlich für Instrument und Orderrichtung vorliegen und unter der Source-Cap von 3x liegen.

Im Runtimepfad verwendete private Abfragen: `Balance`, `TradeBalance`, `OpenOrders`, `OpenPositions`, `TradeVolume`; das geschützte Ordermodul enthält `AddOrder`, wobei das Live-Gate echte Submit-Orders blockiert. `TradesHistory` und `ClosedOrders` verwenden die aktuelle Kraken-Cursor-Paginierung (`with_cursor`/`cursor`), wobei opake Cursor in SQLite persistiert werden. `Ledgers` verwenden weiterhin Offset-Paginierung mit festem Zeitfenster. Die Cursor-Endpunkte werden anhand `cursor.next` beendet – insbesondere wird bei ClosedOrders der API-seitig auf null gesetzte Count nicht fälschlich als Ende oder Vollständigkeitsnachweis interpretiert. Ungültige Items, Cursor-Shape-Fehler und wiederholte Cursor verhindern den Abschluss der aktuellen Generation. Ein `ClosedOrders`-Adapter ist implementiert und wird im Kontosnapshot einschließlich Trade-Details abgefragt; beobachtete offene/geschlossene Order-Rohdaten werden in SQLite über die Order-ID upserted und offene/geschlossene Anzahlen im Runtime-Audit separat protokolliert. Der Kontosnapshot selbst holt den jüngsten Ausschnitt; eine separate persistente Historien-Synchronisierung paginiert ClosedOrders mit fixiertem Zeitfenster und kleinem Seitenbudget pro Zyklus. Die komplette rohe Kontenantwort und jede Position des Snapshots werden zusätzlich unter einer deterministischen Snapshot-ID in SQLite gespeichert, damit Bestände nach einem Neustart prüfbar bleiben; diese Speicherung ist keine selbstbestätigende Portfolio-Reconciliation. Der Backfillstatus wird unabhängig nachgewiesen. Die automatische vollständige Zuordnung zu allen lokalen Intents/Fills, Positionen, Krediten, USDC-Konversionen und Transfers ist damit noch nicht abgeschlossen. Erfolgreiche API-Abfragen oder ein erfolgreicher Historienimport sind kein Beweis einer vollständigen Kontenabstimmung.

## EWR-Finanzierung USD/USDC

Kraken dokumentiert für EWR-Kunden bei USD-Quote-Cash-Paaren eine Margin-Finanzierung in USDC (bei EUR-Quote entsprechend EURC; diese App schließt EUR-Quote-Märkte aus). Erwartete Kreditzwährung aus dem Produktregelwerk ist nicht der Beweis für die tatsächliche Kontoverbindlichkeit: jede Position benötigt bestätigte Kreditwährung, Betrag, Konversions-/Ledgerereignisse und Zeitstempel. USD-Bestand, USDC-Bestand und USDC-Verbindlichkeit sind unterschiedliche Ledgerkonten. Ein pauschales USD=USDC ohne explizite dokumentierte Conversion ist unzulässig.

Offizielle EWR-Information: https://support.kraken.com/articles/margin-conversion-eea

## Kosten

Für Margin-Positionen fallen reguläre Handelsgebühren beim Öffnen und Schließen an. Margin-Eröffnungs- und Rollover-Gebühr werden auf den von Kraken tatsächlich bereitgestellten Finanzierungsbetrag bezogen; Rollover beginnt nach vier Stunden. Kraken erklärt, dass vorhandene Kontosicherheiten nicht von der Gebührenbasis abgezogen werden. Daher verwendet die Vorabkalkulation ohne verifizierte Kreditmenge konservativ den vollen Positionsnotional als Näherung für den erweiterten Betrag. Die Gebührenbasis darf nicht pauschal mit `(1 - 1/leverage)` reduziert werden. Dies ist eine konservative Projektion, keine Behauptung über die tatsächliche positionsbezogene Kreditmenge. Aktuelle Raten können variieren. Die Konto-/Positions-spezifische Margin-Open-/Rollover-Rate ist im Runtimepfad weiterhin nicht zuverlässig belegt; die Kostenschätzung bleibt daher unvollständig und jede neue Risikoerhöhung gesperrt.

Nie einen Logwert wie `4 bps` ungeprüft als tatsächlich angewandte aktuelle Rate ausgeben. Kosten müssen Quelle, Beobachtungszeit, Währung, Einheit, Gebührenbasis, geschätzten/verifizierten Status und angenommene Haltedauer enthalten.

## Öffnen / Reduzieren / Schließen

Die Strategie unterscheidet LONG/SHORT-Aktion und Produktart. `leverage` wird ausschließlich bei einem Margin-Intent übertragen; nicht unterstützte Hebelstufen werden vor API-Übermittlung abgelehnt. Ein fehlender Margin-Short darf nie stillschweigend als Spot-Verkauf ausgeführt werden. `reduce_only` wird nur beim Margin-Schließ-/Reduktionspfad verwendet; die genaue Wirkung muss für jeden API-Pfad anhand der aktuellen Dokumentation und Integrationstests bestätigt werden.

Die Exchange-Antwort für Open Positions enthält Positionsdaten wie Pair, Type, Volumen, geschlossenes Volumen, Kosten, Gebühr, Margin, Wert, Netto-PnL, Status und nächsten Rollover soweit von Kraken zurückgegeben. Diese Felder bilden allein noch kein verifiziertes Ledger oder vollständige Credit-Currency-Attribution.

## Settlement ist keine Market-Close-Order

`settle-position` ist ein eigener Kraken-Ordertyp, mit dem eine Margin-Verpflichtung durch Rückgabe des geliehenen Vermögenswerts ausgeglichen wird. Das Beispiel in Krakens offizieller Supportdokumentation verwendet `volume=0` zur automatische Bestimmung des Abwicklungsvolumens. Für ein robustes System darf diese Abkürzung nicht ungeprüft verwendet werden: vor Settlement die Paar-Positionsliste, Kreditwährung/-betrag und FIFO-Reihenfolge abgleichen; keine lokale Einzelposition als geschlossen markieren, bevor Positions- und Ledgeränderung bestätigt wurden. Settlement darf nicht als normaler Market-Fill und nicht mit gewöhnlicher Trade-Fee gleichgesetzt werden.

## Freigabestatus

Der aktuelle Code hält die echte Orderübermittlung durch ein internes Source-Release-Gate gesperrt. Es bestehen noch keine verifizierten End-to-End-Tests für EWR-Kontoantworten, Settlement-FIFO, Gebührenzuordnung und Recovery nach Submit-/API-Timeout. Daher aktuell kein unbeaufsichtigter Hebelbetrieb.