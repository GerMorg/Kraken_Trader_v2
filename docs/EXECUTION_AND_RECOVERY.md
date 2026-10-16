# Ausführung, Orderzustände und Wiederherstellung

## Einzige Order Authority

Strategielogik darf nur Entscheidungen/Intents erstellen. Die zentrale `OrderAuthority` persistiert jeden ausführbaren Intent vor einem geschützten API-Aufruf, prüft Aktions-/Produkt-/Richtungs-/Hebelparameter und enthält das source-owned Live-Release-Gate. Shadow-Kandidaten werden atomar mit dem terminalen Zustand `SHADOW_ONLY` gespeichert; sie sind nicht als unaufgelöste Ausführungsaufträge sichtbar und können nicht in `SUBMITTING` überführt werden. Ein Validierungsauftrag mit `validate=true` ist ein Börsen-Parametercheck und keine Ausführung. Im aktuellen Repository sendet der Runtime-Zyklus ohnehin keine Orders; jeder Versuch durch `OrderAuthority` ist bis zur formalen Freigabe aus dem Quellcode blockiert.

## Zustandsmaschine

- `INTENT_CREATED`
- `PRECHECK_PASSED`
- `SUBMITTING` oder `VALIDATING`
- `ACKNOWLEDGED`
- Danach erst nach Reconciliation: `OPEN`, `PARTIALLY_FILLED`, `FILLED`, `CANCELED` oder `EXPIRED`
- `REJECTED`, `BLOCKED` und `UNKNOWN_RECONCILING` für Fehler-/Sperrzustände.

Beobachtete offene/geschlossene Orders werden anhand Client-Order-ID und – falls aus einer ACK bekannt – Exchange-Order-ID gegen ungelöste Intents gematcht. Ein eindeutig belegter Zustand wird nur fortgeschrieben, wenn Exchange-Status und Volumen konsistent sind. Mehrdeutige Matches, ungültige Mengen, nicht sichtbare bereits abgesendete Intents und offene externe Orders lösen einen persistenten Circuit Breaker aus; es erfolgt kein blindes Retry. Der Kontosnapshot selbst holt den jüngsten ClosedOrders-Ausschnitt; zusätzlich paginiert eine separate Historien-Synchronisierung TradesHistory und geschlossene Orders mit Kraken-Cursor-Tokens, gespeichert im Datenbankschema und fortgesetzt nach Neustart, mit begrenztem Seitenbudget pro Zyklus. Ledgers bleiben offsetbasiert. Der Backfill bleibt explizit unvollständig, bis alle Seiten derselben Generation nachweislich erfasst sind. Auch vollständige ClosedOrders-Historie löst den noch fehlenden Abgleich mit allen Trades/Fills, Margin-Lots, Funding-Ledgern und Walletbeständen nicht. Order-Reconciliation ist daher ausdrücklich keine vollständige Portfolioreconciliation. Nicht alle Exchangezustände werden automatisch aus Trades History, ClosedOrders und Ledgers in diese State Machine überführt.

## Submit-Timeout und Wiederholungen

Ein Netzwerkfehler bei `AddOrder` kann eintreten, nachdem Kraken die Order bereits angenommen hat. Dieser Fall wird daher als `UNKNOWN_RECONCILING` gespeichert. Es wird keine automatische Wiederholung gesendet. Vor einem erneuten Versuch müssen mindestens Client-Order-ID, offene/geschlossene Orders, Trades History, Open Positions und relevante Ledgerdaten abgeglichen werden. Die Trades-History-Persistenz trennt inzwischen anhand des Kraken-Felds `margin` technisch Spot- und Margin-Fills; fehlende/ungültige Werte blockieren den Backfill. Das ist noch keine PnL- oder steuerliche Reconciliation.

Das ist noch als vollständige End-to-End-Recovery umzusetzen.

## Persistenz

SQLite mit WAL und `synchronous=FULL` speichert Nonce, Events, Kerzen, eindeutige Intent-/Decision-/Client-Order-IDs, beobachtete Börsenorders (`exchange_orders`, einschließlich Rohantwort und letzter Open-/Closed-Beobachtung), Fill-IDs, Ledger-IDs, Steuerereignisse und Circuit Breaker. Zusätzlich werden vollständige rohe Account-Snapshots und normalisierte, pro Snapshot gespeicherte Einzelpositionen (ohne Exchange-Markt-Konsolidierung; Volumen, geschlossene Menge, Kosten, Gebühren, Margin, Wert, Nettofeld, Rollover-Zeit und Terms) persistent gesichert. Der Snapshot ist unveränderliche Audit-Evidenz; er setzt `reconciliation_complete` nie selbst auf true. Beobachtete Exchange-Orders werden bei erneutem Abruf anhand der Order-ID aktualisiert. Die Tabellen machen Daten nicht automatisch vollständig oder korrekt: tatsächliche API-Reconciliation, atomare Reservierung über Prozesse, External-Trades und Crashfenster müssen separat getestet werden.

## Recovery- und Integrationsnachweise vor Live

1. Restart vor/nach Intent-, Submit-, ACK- und Partial-Fill-Zeitpunkt.
2. API-Timeout nach tatsächlicher Orderannahme; keine Doppelorder.
3. verspätete ACK und Exchange-Order mit unbekannter Client-ID.
4. Partial Fill + Cancel; Restmenge korrekt ermitteln.
5. dieselbe Trade-/Ledger-ID mehrfach abfragen; nur einmal verbuchen.
6. manuelle Trades und externe Walletbestände als nicht zuordenbar markieren.
7. mehrere Margin-Lots im selben Paar sowie Settlement-FIFO nachweisen.
8. Cross-Quote-Assets und ausstehende Verkäufe in Walletmengenreservierung integrieren.
9. offene/fehlende Schutzorders und kritischen Marginstatus erkennen.
10. Tests gegen echte, abgeschirmte Test-/Validierungsendpunkte nur ohne echte Ausführung; keine echten Orders innerhalb CI.

Bis diese Kriterien erfüllt und belegt sind, bleibt unbeaufsichtigter Live-Margin-Handel gesperrt.

## Position-level exit decision persistence

Jeder Runtime-Zyklus wertet offene Einzelpositionen mit aktuellen Tickerdaten aus und speichert `POSITION_MANAGEMENT_DECISION` sowohl als Event als auch in der Auditspur. Vorschläge enthalten die Positions-ID, Einstiegsschätzung, relevante Ausstiegsseite, Kursbewegung, Restmenge, vorgeschlagene Menge, Konfiguration und explizit `execution_mode=SHADOW_ONLY`. Auch die Nicht-Entscheidung und nicht unterstützte Positionen werden sichtbar protokolliert. Der Plan erstellt keine Kraken Conditional-Close-Order und setzt keine exchange-native Stop- oder Take-Profit-Order. Die dauerhafte Reconciliation- und Live-Freigabesicherheit wird davon nicht aufgehoben.
