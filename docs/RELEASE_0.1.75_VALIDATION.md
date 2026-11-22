# Version 0.1.75 — Architektur- und Release-Grenzen

## Ziel der Bereinigung

Der normale Home-Assistant-Zyklus soll sich auf aktuelle Markt-/Kontodaten, eine produktgetrennte Entscheidung, eine zentrale Ausführungsautorität, börsenseitigen Schutz und Reconciliation beschränken. Forschung wird nicht parallel in jedem Handelszyklus neu berechnet.

## Bedienung

Die einzige benutzerseitige Ausführungseinstellung heißt `real_orders_enabled`. Standard und Fallback bei fehlender/alter Option ist `false`. Alte Werte `mode`, `live_enabled`, `kill_switch` und `validate_shadow_orders` werden ignoriert. Alte Datenbanken und bereits gespeicherte Research-Ereignisse werden nicht gelöscht oder zurückgesetzt.

Die interne Konstante `INTERNAL_LIVE_EXECUTION_RELEASED` bleibt bis zur Erfüllung der produktbezogenen Release-Kriterien `false`. Der Operator-Schalter allein kann echte Orders deshalb noch nicht freischalten.

## Spot-/Spot-Margin-Schutz

Neue Spot-/Spot-Margin-Einstiegs- und Erhöhungsorders senden in demselben Kraken-`AddOrder`-Request eine `trailing-stop` Conditional-Close-Order. Der relative Offset stammt aus der volatilitätsabhängigen Stopdistanz des konkreten Signals (derzeit 1.5–5.0%); 3.0% ist der Rückfallwert ohne Signalwert. Die API-Antwort muss eine nichtleere `descr.close`-Beschreibung enthalten.

Fehlt die Schutzbestätigung nach einer angenommenen Primärorder, bleibt der Intent `ACKNOWLEDGED`, der persistente `PROTECTIVE_ORDER`-Circuit-Breaker wird gesetzt, und neue Risikoerhöhungen bleiben gesperrt. Die App darf die bestätigte Primärorder nicht fälschlich als abgelehnt verbuchen.

### Nicht freigegebene Schutzfälle

Die aktuelle Spot-REST-AddOrder-Spezifikation führt `trailing-stop` ausdrücklich als zulässigen Wert für `close[ordertype]` auf. Sie stellt zugleich klar, dass die ausgelöste Conditional-Close-Order anschließend unabhängig ist und den Nettobestand reduzieren oder erhöhen kann. Eine manuelle oder externe Schließung der ursprünglichen Position garantiert daher nicht, dass die bedingte Gegenorder verschwindet; bleibt sie offen und wird später ausgelöst, kann sie eine Gegenposition aufbauen.

Kraken dokumentiert das Conditional-Close-Volumen als gleich dem Volumen der Primärorder. Verhalten bei Teilfüllung, Abbruch, Restmenge und bereits bestehender Position muss durch echte Demo-End-to-End-Tests belegt werden. Eine Conditional-Close-Order wird außerdem nicht automatisch durch eine unabhängige manuelle Schließung entfernt. Vor einer Freigabe echter App-Einstiege ist daher ein separater Protection-Manager erforderlich, der:

- tatsächliche Positionen und kumulierte Fills abgleichen,
- den exakten verbleibenden Schutzumfang feststellen,
- überzählige/alte Stops sicher amendieren oder stornieren,
- bei nicht eindeutigem Zustand neue Risikoerhöhungen sperren,
- Restart, API-Timeout, Teilfill/Cancel und manuelle Schließung abdecken.

Diese Punkte sind in 0.1.75 noch offen. Das interne Live-Release-Gate bleibt geschlossen.

## Futures

Der Produktions-Futures-Client ist weiterhin Read-only. Diese Version enthält keinen Futures-Order-, Amend- oder Cancel-Lifecycle. Für Futures sind ein eigener Trading-Adapter, eine Futures-spezifische Entscheidungs-/Risikostruktur, Demo-Verifikation, eigene Fill-/Funding-Buchhaltung und eine eigene Reduce-only-Schutzorder erforderlich. Kein Spot-Stopparameter darf in die Futures API übernommen werden.

## CPU und Research

Der normale Zyklus startet keinen Microstructure-WebSocket und verarbeitet keine großen Strategy-Lab-, Forward-Matching- oder gemeinsamen Portfolio-Research-Auswertungen. Persistierte Research-Daten bleiben erhalten. Das beseitigt einen wesentlichen dauerhaften Verbraucher, ist aber keine gemessene CPU-Garantie. Nach Installation sind tatsächliche CPU-Last, Cycle-Dauer, WebSocket-Status (muss disabled sein) und Speicher-/Datenbankverhalten in Home Assistant zu prüfen.

## Hebel

Die Hebelwahl verwendet den höchsten tatsächlich für Instrument und Richtung unterstützten Hebel, maximal jedoch 3x. Das ist keine Gebührenermäßigung: Gebühren bleiben auf den Nominalwert bezogen. Positionsgröße, Risikobudget, Collateral, Portfolio-Exposure, Tagesverlust und Drawdown begrenzen das Risiko weiterhin unabhängig vom Hebel.

## Validierung vor Produktivfreigabe

- [ ] Vollständige CI auf dem finalen PR-Head: Ruff, Mypy, Pytest, Security/Dependency-Audit, Python compile, AppArmor, HA-Struktur und Container-Smoke-Test
- [ ] Spot-/Margin Conditional-Close: Long/Short, beide Richtungen, unterschiedliche Stop-Offsets und Exchange-Antwort-Parsing
- [ ] Demo-Partial-Fill + Cancel, Verifikation der tatsächlichen Schutzmenge
- [ ] Restart/API-Timeout zwischen Primary Submit und Exchange-Antwort
- [ ] Manuelle Schließung mit noch existierender Conditional-Close-Order
- [ ] Manuell angelegte offene Orders ohne falschen Bot-Reconciliation-Breaker
- [ ] Futures Demo Order-/Cancel-/Amend-/Reduce-only-Schutz-Lifecycle
- [ ] Getrennte Spot-/Futures-Reconciliation, Gebühren, Funding und Steuerbuchhaltung
- [ ] Mehrere reale HA-Zyklen mit bestätigter CPU-Reduktion und stabiler Zyklusdauer

Keine der noch offenen Schutz-/Futures-Funktionen darf durch bloßes Setzen von `real_orders_enabled` übergangen werden.
