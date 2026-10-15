# Ausführung, Orderzustände und Wiederherstellung

## Einzige Order Authority

Strategielogik darf nur Entscheidungen/Intents erstellen. Die zentrale `OrderAuthority` persistiert jeden Intent vor einem geschützten API-Aufruf, prüft Aktions-/Produkt-/Richtungs-/Hebelparameter und enthält das source-owned Live-Release-Gate. Ein Validierungsauftrag mit `validate=true` ist ein Börsen-Parametercheck und keine Ausführung. Im aktuellen Repository sendet der Runtime-Zyklus ohnehin keine Orders; jeder Versuch durch `OrderAuthority` ist bis zur formalen Freigabe aus dem Quellcode blockiert.

## Zustandsmaschine

- `INTENT_CREATED`
- `PRECHECK_PASSED`
- `SUBMITTING` oder `VALIDATING`
- `ACKNOWLEDGED`
- Danach erst nach Reconciliation: `OPEN`, `PARTIALLY_FILLED`, `FILLED`, `CANCELED` oder `EXPIRED`
- `REJECTED`, `BLOCKED` und `UNKNOWN_RECONCILING` für Fehler-/Sperrzustände.

Nicht alle Exchangezustände werden derzeit bereits automatisch aus Trades History, ClosedOrders und Ledgers in diese State Machine überführt. Es ist deshalb keine Behauptung einer vollständigen automatischen Recovery.

## Submit-Timeout und Wiederholungen

Ein Netzwerkfehler bei `AddOrder` kann eintreten, nachdem Kraken die Order bereits angenommen hat. Dieser Fall wird daher als `UNKNOWN_RECONCILING` gespeichert. Es wird keine automatische Wiederholung gesendet. Vor einem erneuten Versuch müssen mindestens Client-Order-ID, offene/geschlossene Orders, Trades History, Open Positions und relevante Ledgerdaten abgeglichen werden. Das ist noch als vollständige End-to-End-Recovery umzusetzen.

## Persistenz

SQLite mit WAL und `synchronous=FULL` speichert Nonce, Events, Kerzen, eindeutige Intent-/Decision-/Client-Order-IDs, Fill-IDs, Ledger-IDs, Steuerereignisse und Circuit Breaker. Die Tabellen machen Daten nicht automatisch vollständig oder korrekt: tatsächliche API-Reconciliation, atomare Reservierung über Prozesse, External-Trades und Crashfenster müssen separat getestet werden.

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