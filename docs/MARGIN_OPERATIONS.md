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

## Am 16.10.2026 gegen offizielle Kraken-Dokumentation geprüft

- [EWR-Margin-Konvertierungen](https://support.kraken.com/articles/margin-conversion-eea), zuletzt aktualisiert am 20.01.2026: USD-Quote-Cash-Paare werden für EWR-Kunden mit USDC finanziert. Kraken nennt die tatsächliche Kreditwährung und den Kreditbetrag in Transaktionsdetails; offene Positionen zeigen die stablecoin-finanzierte Position ebenfalls. Die beschriebene automatische Umrechnung wird mit 1:1 und ohne Umrechnungsgebühr angegeben. Das ist eine belegte Eigenschaft des konkreten Kraken-Konvertierungsvorgangs, **keine** allgemeine Gleichsetzung aller USD-/USDC-Guthaben oder Marktpreise. Die tatsächliche positionsbezogene Conversion muss weiter mit Rohantwort und Ledger verknüpft werden.
- [Margin-Gebühren](https://support.kraken.com/at/articles/206161568-what-are-the-fees-opening-and-rollover-for-trading-using-margin-), zuletzt aktualisiert am 28.07.2026: Zusätzlich zur üblichen Spot-Handelsgebühr auf Öffnungs- und Schließungsvolumen fallen Margin-Eröffnungs- und Rollover-Gebühren an. Die Finanzierungsgebühr basiert auf dem erweiterten Betrag; bereits vorhandene Kontosicherheiten werden nicht davon abgezogen. Rollover beginnt nach vier Stunden, der Rollover-Satz wird bei Ausführung festgeschrieben und ist markt-/instrumentabhängig. Ein Margin-Settlement hat laut Kraken keine normale Trade-Gebühr. Für Zwangsliquidationen nennt Kraken eine Liquidationsgebühr von 2 % und keine zusätzliche Standard-Trade-Gebühr, da die Liquidation nicht über das Orderbuch erfolgt.
- [Get Open Positions](https://docs.kraken.com/api-reference/account-data/get-open-positions): die API-Beispiele enthalten u. a. `pair`, `type`, `cost`, `fee`, `vol`, `vol_closed`, `margin`, `value`, `net`, `terms` und `rollovertm`. Diese Felder helfen beim Positionsabgleich, beweisen für sich allein aber weder die vollständige Kreditzuordnung noch den tatsächlichen USDC-Ledgerpfad. `terms` muss als Börsenevidenz unverändert erhalten bleiben, nicht als allgemeingültiger zukünftiger Satz interpretiert werden.
- [Add Order](https://docs.kraken.com/api-reference/trading/add-order): dokumentiert `leverage`, `reduce_only`, den Ordertyp `settle-position` und `validate=true` als reine Validierung ohne Ausführung. Das ist kein Fill-Nachweis. Die genaue Wirkung bei einer konkreten Konto-/Produktkonstellation muss in einer autorisierten Integration verifiziert sein, bevor ein Submit-Pfad freigegeben wird.

### Unmittelbare Konsequenz für die Implementierung

Der vorliegende Source-Gate bleibt geschlossen. Ein fehlender oder nicht instrument-/positionsbezogen verifizierbarer aktueller Finanzierungswert darf vor einer neuen Margin-Risikoerhöhung nicht durch einen alten festen Satz ersetzt werden. Nach einer erfolgreichen Margin-Ausführung müssen die tatsächliche Kreditwährung, der Betrag, die Quelle und Zeitstempel der Gebühren-/Terms-Evidenz sowie alle zugehörigen Conversion-/Ledger-Ereignisse positionsbezogen gespeichert werden. Können sie nicht zusammengeführt werden, bleibt die Positions- und Nettokosten-Reconciliation `INCOMPLETE`; die App darf daraus weder eine vollständige Portfolioabstimmung noch einen bestätigten Netto-PnL ableiten.

## Markt- und API-Verhalten

Die App verwendet den aktuellen AssetPairs-Bestand, nicht fest programmierte Hebelstufen. Es werden nur online Krypto-Spot-Märkte mit USD-Quote aufgenommen. Tokenisierte Aktien und nicht USD-notierte Märkte sind ausgeschlossen. `leverage_buy` und `leverage_sell` werden separat betrachtet; die gewählte Stufe muss tatsächlich für Instrument und Orderrichtung vorliegen und unter der Source-Cap von 3x liegen.

Im Runtimepfad verwendete private Abfragen: `Balance`, `TradeBalance`, `OpenOrders`, `OpenPositions` wird ohne `consolidation=market` abgefragt, damit einzelne Positions-IDs und mehrere Lots desselben Paares erhalten bleiben; außerdem `TradeVolume`. Das geschützte Ordermodul enthält `AddOrder`, wobei das Live-Gate echte Submit-Orders blockiert. `TradesHistory` und `ClosedOrders` verwenden die aktuelle Kraken-Cursor-Paginierung (`with_cursor`/`cursor`), wobei opake Cursor in SQLite persistiert werden. `Ledgers` verwenden weiterhin Offset-Paginierung mit festem Zeitfenster. Historische OHLCV-Szenarien nutzen dieselbe konservative volle Notional-Basis für Margin-Open-/Rolloverkosten und dürfen ohne historische produktspezifische Gebühren-/Kreditdaten nicht als realistische Backtests ausgelegt werden. Die Cursor-Endpunkte werden anhand `cursor.next` beendet – insbesondere wird bei ClosedOrders der API-seitig auf null gesetzte Count nicht fälschlich als Ende oder Vollständigkeitsnachweis interpretiert. Ungültige Items, Cursor-Shape-Fehler und wiederholte Cursor verhindern den Abschluss der aktuellen Generation. Ein `ClosedOrders`-Adapter ist implementiert und wird im Kontosnapshot einschließlich Trade-Details abgefragt; beobachtete offene/geschlossene Order-Rohdaten werden in SQLite über die Order-ID upserted und offene/geschlossene Anzahlen im Runtime-Audit separat protokolliert. Der Kontosnapshot selbst holt den jüngsten Ausschnitt; eine separate persistente Historien-Synchronisierung paginiert ClosedOrders mit fixiertem Zeitfenster und kleinem Seitenbudget pro Zyklus. Die komplette rohe Kontenantwort und jede Position des Snapshots werden zusätzlich unter einer deterministischen Snapshot-ID in SQLite gespeichert, damit Bestände nach einem Neustart prüfbar bleiben; diese Speicherung ist keine selbstbestätigende Portfolio-Reconciliation. Der Backfillstatus wird unabhängig nachgewiesen. Die automatische vollständige Zuordnung zu allen lokalen Intents/Fills, Positionen, Krediten, USDC-Konversionen und Transfers ist damit noch nicht abgeschlossen. Selbst bei bestätigtem Eröffnungsfill wird eine Position mit `vol_closed > 0` nicht als vollständig positionsabgestimmt ausgewiesen, bis das tatsächliche Reduktions-Fill eindeutig zugeordnet ist; willkürliche FIFO-Verteilung wird nicht angenommen. Erfolgreiche API-Abfragen oder ein erfolgreicher Historienimport sind kein Beweis einer vollständigen Kontenabstimmung.

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

`settle-position` ist ein eigener Kraken-Ordertyp, mit dem eine Margin-Verpflichtung durch Rückgabe des geliehenen Vermögenswerts ausgeglichen wird. Krakens Supportdokumentation beschreibt ausdrücklich die Auswahl der ganz oder teilweise abzuwickelnden Menge und die FIFO-Reihenfolge; bei mehreren Positionen desselben Paares wird die älteste zuerst abgewickelt. Die AddOrder-API erwähnt `volume=0` für Margin-Schließorders, doch der App-Settlementpfad verwendet diesen Sonderwert nicht: er verlangt eine positive, nachweisbare Menge und eine Evidenzkette mit konkreter Positions-ID, Währung, Kreditbetrag, Snapshot und abgestimmtem Ledger. Vor der tatsächlichen Freigabe muss außerdem auf einem Konto ohne echte Orderausführung bestätigt werden, wie die verfügbare API die gewünschte Settlement-Menge für dieses Produkt validiert. Keine lokale Position wird als geschlossen markiert, bevor OpenPositions- und Ledgeränderung bestätigt wurden. Settlement darf nicht als normaler Market-Fill und nicht mit gewöhnlicher Trade-Fee gleichgesetzt werden.

## Freigabestatus

Der aktuelle Code hält die echte Orderübermittlung durch ein internes Source-Release-Gate gesperrt. Es bestehen noch keine verifizierten End-to-End-Tests für EWR-Kontoantworten, Settlement-FIFO, Gebührenzuordnung und Recovery nach Submit-/API-Timeout. Daher aktuell kein unbeaufsichtigter Hebelbetrieb.