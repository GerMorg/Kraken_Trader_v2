# Variante E — Orderflow & Market Microstructure

## Zweck und Sicherheitsgrenze

Variante E ergänzt ab App-Version 0.1.55; die WebSocket-v2-Symbolübersetzung und Subscription-Diagnose wurden in 0.1.56 korrigiert. eine eigenständige, kontinuierliche Datenerfassung für Kraken Spot USD-Kryptopaare. Ziel ist es, Mikrostrukturinformationen für spätere Forschung zu sammeln, die aus H1-Schlusskursen oder einzelnen REST-Orderbuch-Snapshots nicht hervorgehen.

**Der aktuelle Status ist Datenerfassung, nicht Strategiehandel.** Der Collector erzeugt keine Handelssignale, Order-Intents, Orders oder Fills. Er ist nicht mit der Order Authority verbunden. Er verändert weder TSM noch die Varianten A–D und darf keine Parameter oder Live-Freigaben promotieren. INTERNAL_LIVE_EXECUTION_RELEASED bleibt unverändert False.

Der öffentliche Feed ist unauthentifiziert. Die Erfassung benötigt keine privaten API-Rechte und verwendet ausschließlich die regulären USD-Quote-Kryptopaare aus dem dynamischen Runtime-Universum.

## Kraken-WebSocket-v2-Daten

Endpoint: wss://ws.kraken.com/v2.

Das interne Universum stammt aus den Kraken-AssetPairs-Metadaten. REST-`wsname` und WebSocket-v2-Symbole sind jedoch nicht vollständig identisch: Kraken WebSocket v2 verwendet für Bitcoin `BTC/USD`, während REST-`wsname` `XBT/USD` liefern kann. Die App übersetzt deshalb `XBT/USD` beim Subscribe zu `BTC/USD` und mappt eingehende `BTC/USD`-Events zurück auf den internen Schlüssel `XBT/USD`. Das unveränderte Weiterreichen von REST-`wsname` an WebSocket v2 ist ausdrücklich nicht zulässig.

Abonniert werden:

- book mit Tiefe 10 und Snapshot beim Subscribe. Es verarbeitet die vollständigen Snapshots und danach die geordneten Preislevel-Änderungen. Menge null entfernt ein Preislevel.
- trade mit Snapshot deaktiviert. Die Nachrichten werden als einzelne Trades verarbeitet; die side-Angabe beschreibt die Taker-Seite. Die Trade-ID wird pro Markt auf Lücken und nicht-monotone Werte geprüft.

Der Worker bleibt über die normalen Runtime-Zyklen hinweg verbunden. Er verfolgt Kraken-Subscription-Acknowledgements getrennt je Symbol und Channel; die API sendet je Symbol eine eigene Bestätigung. Statuswerte unterscheiden `SUBSCRIBING`, `CONNECTED` und `CONNECTED_PARTIAL`. Ein einzelner abgelehnter Markt beendet nicht mehr die gesamte Verbindung. Die begrenzte Kraken-Fehlermeldung sowie Channel und Symbol werden im `CYCLE_SUMMARY` angezeigt. Fehlende Bestätigungen führen nach einem Timeout zu einem Backoff-Reconnect statt zu schnellen Wiederholungen. Ein geändertes Symboluniversum führt zu einer kontrollierten Neuverbindung. Netzwerkfehler oder ein Datenfeed-Ausfall dürfen den Handelszyklus nicht blockieren. Nach Verbindungsverlust wird der bisherige Book-Zustand als ungültig markiert; erst ein neuer Snapshot mit erfolgreichem Checksum stellt wieder einen gültigen Zustand her.

Der CRC32-Checksum des Orderbuchs wird vor der Verwendung geprüft. Es werden die zehn besten Ask-Level aufsteigend, anschließend die zehn besten Bid-Level absteigend verarbeitet. Bei Abweichung wird die Verbindung erneuert, um mit einem frischen Snapshot zu beginnen. Preis- und Mengenwerte werden als Decimal behandelt, um Rundungsfehler zu vermeiden.

## Minutenfeatures

Die Rohereignisse werden im Arbeitsspeicher verarbeitet und pro UTC-Minute aggregiert. Es werden keine vollständigen WebSocket-Rohnachrichten dauerhaft archiviert.

| Feature | Bedeutung |
|---|---|
| buy_notional_usd, sell_notional_usd | Taker-Buy- und Taker-Sell-Volumen in USD, aus Preis × Menge |
| signed_trade_notional_usd | Signiertes Trade-Notional: Buy minus Sell |
| trade_imbalance | Signiertes Trade-Notional geteilt durch gesamtes beobachtetes Trade-Notional |
| ofi_l1_base | Aggregierter L1-Orderflow-Imbalance-Proxy aus Änderungen des besten Bid-/Ask-Preis- und Mengenlevels |
| ofi_l1_imbalance | OFI-Proxy relativ zur Summe der beobachteten vorherigen besten Bid-/Ask-Mengen |
| spread_bps | Relativer Spread zwischen bestem Ask und Bid |
| bid_depth_usd, ask_depth_usd | Sichtbares Quote-Notional in den bis zu zehn gespeicherten Levels |
| depth_imbalance | Bid-Tiefe minus Ask-Tiefe, geteilt durch die gesamte sichtbare Tiefe |
| microprice_deviation_bps | Abweichung des mengen-gewichteten Microprice vom Midprice |
| checksum_checks, checksum_failures | Anzahl der Checksum-Prüfversuche und fehlgeschlagener Prüfsummen |
| trade_id_gaps | Anzahl erkannter übersprungener Trade-IDs innerhalb des beobachteten Streams |
| connection_gap_count | Verbindungsunterbrechungen, die eine Minutenaggregation betreffen |

Die L1-OFI- und Microprice-Kennzahlen sind beobachtungsbasierte Mikrostrukturfeatures. Sie sind keine ausführbare Preisprognose und keine Garantie für eine künftige Orderausführung. Sichtbare Tiefe beweist weder, dass die angezeigte Liquidität bestehen bleibt, noch dass eine konkrete Order vollständig ausgeführt würde.

## Datenqualität und Speicherung

Die SQLite-Tabelle microstructure_bars enthält eine Zeile je Paar und Minute. Primärschlüssel (pair, minute_ts) verhindert doppelte Speicherung. Die Daten werden append-only geschrieben; ältere Handels-, Order-, Fill-, Ledger-, Steuer- und Research-Daten werden nicht ersetzt.

- VALID: Ein Book-Zustand wurde mit Checksum verifiziert, ist für die Minute ausreichend frisch und für diese Minute wurden keine Checksum-Fehler, erkannten Trade-ID-Lücken, unbekannten initialen Trade-ID-Baselines oder Verbindungsunterbrechungen registriert.
- PARTIAL: Mindestens eine dieser Bedingungen ist nicht erfüllt oder es liegen nur unvollständige Beobachtungen vor.

Eine Minute ohne Trades ist nicht automatisch fehlerhaft: Trade-Anzahl und Trade-Notional bleiben null. Eine Minute ohne verifiziertes Orderbuch kann dagegen nicht als vollständige Orderflow-Beobachtung gelten.

CYCLE_SUMMARY weist den Verbindungsstatus, REST- und WebSocket-Symbole, erwartete/bestätigte/abgelehnte/offene Subscriptions, Subscriptionsabdeckung, die letzte Kraken-Fehlermeldung, gespeicherte Minuten-Bars, die persistierte Gesamtzahl, Checksum-Fehler, Trade-ID-Lücken, Verbindungsunterbrechungen und den letzten Fehlertyp aus. Die HA-Optionen microstructure_enabled (Standard true) und microstructure_max_symbols (Standard 12, begrenzt auf 1–20) steuern die Erfassung.

Die Migration auf SQLite-Schema 13 ist additiv. Ein bestehender Datenbankinhalt wird nicht zurückgesetzt.

## Grenzen der Daten und nächste Forschungsstufe

Kraken-WebSocket liefert aktuelle Ereignisse, keine historische Orderflow-Rücklieferung. Die App kann daher keine Mikrostrukturfeatures vor der Installation bzw. Aktivierung rekonstruieren. Auch nach einem Neustart oder Netzwerkverlust bleiben Feed-Lücken als Qualitätsgrenzen sichtbar; sie werden nicht künstlich aufgefüllt.

Eine spätere Strategieauswertung muss auf den Minutenfeatures chronologische Train-/Validation-/unangetastete Testsegmente mit Purging und einem separaten Forward-Shadow-Test verwenden. Sie muss Nicht-Signale einschließen, Kosten und Ausführungseffekte getrennt ausweisen, die zeitliche Korrelation der Signale berücksichtigen und L2-Qualität/Feed-Lücken als Ausschluss- oder Robustheitskriterien behandeln. Erst danach kann entschieden werden, ob die Features für eine eigenständige Variante E einen inkrementellen Nettovorteil gegenüber TSM, A–D oder einem einfachen Baseline-Modell liefern.

Eine positive Mikrostruktur-Metrik allein ist kein Beleg für Profitabilität. E bleibt daher bis zur gesonderten Validierung eine reine Beobachtungs- und Forschungsstrecke.

## Offizielle Kraken-Referenzen

- WebSocket v2 Book Channel: https://docs.kraken.com/api/docs/websocket-v2/book/
- WebSocket v2 Trade Channel: https://docs.kraken.com/api/docs/websocket-v2/trade/
- WebSocket v2 Book Checksum Guide: https://docs.kraken.com/api/docs/guides/spot-ws-book-v2/
- Kraken WebSocket FAQ: https://support.kraken.com/articles/360022326871-websocket-api-frequently-asked-questions-faq-



## v0.1.61 — Erweiterte, qualitätsbegrenzte Minutenbeobachtungen

Die WS-v2-Minutenaggregate speichern zusätzlich Trade-OHLC (aus beobachteten öffentlichen Trades), Basisvolumen, erste/letzte Trade-Zeit, den letzten verifizierten Midprice, die letzten bis zu zehn Bid-/Ask-Level sowie Anzahl und Zeitspanne verifizierter Book-Ereignisse. Die Level werden als Dezimalstrings in derselben unveränderlichen JSON-Nutzlast persistiert; bestehende Zeilen bleiben unverändert lesbar und es ist keine Schema-Migration erforderlich.

Ein Minutenbalken wird nur dann als `VALID` markiert, wenn mindestens zwei verifizierte Book-Ereignisse mindestens zehn Sekunden auseinanderliegen und alle bisherigen Integritätsbedingungen erfüllt sind. Andernfalls ist er `PARTIAL`. Das ist eine Mindestabdeckung, kein Beweis kontinuierlicher Feed-Abdeckung.

**Wichtige Grenze:** Der gespeicherte L2-Snapshot ist der letzte verifizierte Zustand innerhalb der Minute, kein vollständiger zeitaufgelöster Orderbook-Tape. Er erlaubt daher noch keinen exakten historischen Book-Walk zum nächsten Minuten-Open. Ein späterer Research-Runner muss Snapshot-Zeitstempel, Alter, Sequenz, Fill-Quote und Datenqualität explizit prüfen. Die neuen Felder sind Datengrundlage, keine Strategie, kein Fill, kein Portfolio-PnL und keine Live-Freigabe.


## v0.1.62 — Zielzeit-Markierungen und Research-Portfolios

Forward-Beobachtungen werden zusätzlich getrennt von der bisherigen H1-Schlusskursauswertung bewertet. Als Referenz dient ausschließlich der letzte Minuten-Midprice aus einem `VALID`-Balken zum oder vor dem Zielzeitpunkt, dessen Book-Samples, zeitlicher Abstand, CRC-Prüfungen und Feed-Qualität die v0.1.61-Anforderungen erfüllen. Der Book-Zeitstempel darf höchstens 60 Sekunden vor dem prognostizierten Zielzeitpunkt liegen. Ein nach dem Zielzeitpunkt entstandener Book-Snapshot wird niemals verwendet; dadurch wird Look-ahead-Bias in dieser Mark-Diagnose vermieden. Der Bericht misst die Abweichung und Match-Abdeckung; ein Treffer ist weiterhin ein Midprice-Mark, kein ausführbarer Fill. Historische `VALID`-Zeilen ohne die neuen Sample-Felder werden nicht nachträglich als qualifiziert behandelt.

Die gemeinsame Strategie-Simulation verarbeitet nur gematchte, vollständig kostenbewertete Kandidaten. Kapital, Markt-Exklusivität, Brutto-/Nettoexposure, Positionsgröße und Positionsanzahl gelten gemeinsam. Konfligierende Kandidaten desselben Marktes zur selben Entscheidungszeit werden sämtlich ausgeschlossen statt nachträglich den Gewinner auszuwählen. Shorts werden nur auf Märkten einbezogen, deren aktuelle Kraken-Instrumentmetadaten Margin-Sells erlauben. Zusätzliche Margin-Kostenbudgets sind Sensitivitätsannahmen und keine Kraken-Fee-Quotes.

Die Simulation berechnet hypothetische, realisierte Label-PnL auf normiertem Research-Kapital. Sie ist noch kein vollständiger Portfolio-Backtest: Cross-Asset-Korrelation, Intratrade-Marktwertpfad, Stop-/Take-Profit-Ausführung, Margin-Collateral, tatsächliche Finanzierungskosten, Liquidationen und Orderbuch-Fills fehlen. Es gibt keine Strategiepromotion und keine Orderfreigabe.
