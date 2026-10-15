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

Im Runtimepfad verwendete private Abfragen: `Balance`, `TradeBalance`, `OpenOrders`, `OpenPositions`, `TradeVolume`; das geschützte Ordermodul enthält `AddOrder`, wobei der Live-Gate eine echte Submit-Order blockiert. Client-Wrapper für `TradesHistory` und `Ledgers` sind verfügbar, werden aber noch nicht paginiert regelmäßig abgerufen, idempotent persistiert und mit Positionen/Transfers abgestimmt. Ein `ClosedOrders`-Wrapper und die vollständige Reconciliation fehlen. Ein erfolgreicher API-Abruf ist kein Beweis einer vollständigen Kontenabstimmung.

## EWR-Finanzierung USD/USDC

Kraken dokumentiert für EWR-Kunden bei USD-Quote-Cash-Paaren eine Margin-Finanzierung in USDC (bei EUR-Quote entsprechend EURC; diese App schließt EUR-Quote-Märkte aus). Erwartete Kreditzwährung aus dem Produktregelwerk ist nicht der Beweis für die tatsächliche Kontoverbindlichkeit: jede Position benötigt bestätigte Kreditwährung, Betrag, Konversions-/Ledgerereignisse und Zeitstempel. USD-Bestand, USDC-Bestand und USDC-Verbindlichkeit sind unterschiedliche Ledgerkonten. Ein pauschales USD=USDC ohne explizite dokumentierte Conversion ist unzulässig.

Offizielle EWR-Information: https://support.kraken.com/articles/margin-conversion-eea

## Kosten

Für Margin-Positionen fallen reguläre Handelsgebühren beim Öffnen und Schließen an. Margin-Eröffnungs- und Rollover-Gebühr werden auf den von Kraken tatsächlich bereitgestellten Finanzierungsbetrag bezogen; Rollover wird nach den dokumentierten Zeitintervallen belastet. Aktuelle Rate und Ausführungsbedingungen können variieren. Die App holt die Gebührenstufe aus TradeVolume, sofern sie verfügbar ist, aber eine kontoabhängige, positionsbezogene Margin-Open-/Rollover-Rate ist im aktuellen Runtimepfad noch nicht zuverlässig belegt. In diesem Fall bleibt die Kostenschätzung unvollständig und die Entry-Entscheidung gesperrt.

Nie einen Logwert wie `4 bps` ungeprüft als tatsächlich angewandte aktuelle Rate ausgeben. Kosten müssen Quelle, Beobachtungszeit, Währung, Einheit, Gebührenbasis, geschätzten/verifizierten Status und angenommene Haltedauer enthalten.

## Öffnen / Reduzieren / Schließen

Die Strategie unterscheidet LONG/SHORT-Aktion und Produktart. `leverage` wird ausschließlich bei einem Margin-Intent übertragen; nicht unterstützte Hebelstufen werden vor API-Übermittlung abgelehnt. Ein fehlender Margin-Short darf nie stillschweigend als Spot-Verkauf ausgeführt werden. `reduce_only` wird nur beim Margin-Schließ-/Reduktionspfad verwendet; die genaue Wirkung muss für jeden API-Pfad anhand der aktuellen Dokumentation und Integrationstests bestätigt werden.

Die Exchange-Antwort für Open Positions enthält Positionsdaten wie Pair, Type, Volumen, geschlossenes Volumen, Kosten, Gebühr, Margin, Wert, Netto-PnL, Status und nächsten Rollover soweit von Kraken zurückgegeben. Diese Felder bilden allein noch kein verifiziertes Ledger oder vollständige Credit-Currency-Attribution.

## Settlement ist keine Market-Close-Order

`settle-position` ist ein eigener Kraken-Ordertyp, mit dem eine Margin-Verpflichtung durch Rückgabe des geliehenen Vermögenswerts ausgeglichen wird. Das Beispiel in Kr akens offizieller Supportdokumentation verwendet `volume=0` zur automatische Bestimmung des Abwicklungsvolumens. Für ein robustes System darf diese Abkürzung nicht ungeprüft verwendet werden: vor Settlement die Paar-Positionsliste, Kreditwährung/-betrag und FIFO-Reihenfolge abgleichen; keine lokale Einzelposition als geschlossen markieren, bevor Positions- und Ledgeränderung bestätigt wurden. Settlement darf nicht als normaler Market-Fill und nicht mit gewöhnlicher Trade-Fee gleichgesetzt werden.

## Freigabestatus

Der aktuelle Code hält die echte Orderübermittlung durch ein internes Source-Release-Gate gesperrt. Es bestehen noch keine verifizierten End-to-End-Tests für EWR-Kontoantworten, Settlement-FIFO, Gebührenzuordnung und Recovery nach Submit-/API-Timeout. Daher aktuell kein unbeaufsichtigter Hebelbetrieb.