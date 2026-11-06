# Variante E — Orderflow & Market Microstructure

## Zweck und Sicherheitsgrenze

Variante E ergänzt ab App-Version 0.1.55 eine eigenständige, kontinuierliche Datenerfassung für Kraken Spot USD-Kryptopaare. Ziel ist es, Mikrostrukturinformationen für spätere Forschung zu sammeln, die aus H1-Schlusskursen oder einzelnen REST-Orderbuch-Snapshots nicht hervorgehen.

**Der aktuelle Status ist Datenerfassung, nicht Strategiehandel.** Der Collector erzeugt keine Handelssignale, Order-Intents, Orders oder Fills. Er ist nicht mit der Order Authority verbunden. Er verändert weder TSM noch die Varianten A–D und darf keine Parameter oder Live-Freigaben promotieren. INTERNAL_LIVE_EXECUTION_RELEASED bleibt unverändert False.

Der öffentliche Feed ist unauthentifiziert. Die Erfassung benötigt keine privaten API-Rechte und verwendet ausschließlich die regulären USD-Quote-Kryptopaare aus dem dynamischen Runtime-Universum.

## Kraken-WebSocket-v2-Daten

Endpoint: wss://ws.kraken.com/v2.

Die Symbolbezeichnungen stammen direkt aus dem Kraken-AssetPairs-Feld wsname; sie werden nicht aus den REST-Tickern oder den internen Asset-Codes selbst konstruiert.

Abonniert werden:

- book mit Tiefe 10 und Snapshot beim Subscribe. Es verarbeitet die vollständigen Snapshots und danach die geordneten Preislevel-Änderungen. Menge null entfernt ein Preislevel.
- trade mit Snapshot deaktiviert. Die Nachrichten werden als einzelne Trades verarbeitet; die side-Angabe beschreibt die Taker-Seite. Die Trade-ID wird pro Markt auf Lücken und nicht-monotone Werte geprüft.

Der Worker bleibt über die normalen Runtime-Zyklen hinweg verbunden. Ein geändertes Symboluniversum führt zu einer kontrollierten Neuverbindung. Netzwerkfehler oder ein Datenfeed-Ausfall dürfen den Handelszyklus nicht blockieren. Nach Verbindungsverlust wird der bisherige Book-Zustand als ungültig markiert; erst ein neuer Snapshot mit erfolgreichem Checksum stellt wieder einen gültigen Zustand her.

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

CYCLE_SUMMARY weist den Verbindungsstatus, die Zahl abonnierter Symbole, gespeicherte Minuten-Bars, die persistierte Gesamtzahl, Checksum-Fehler, Trade-ID-Lücken, Verbindungsunterbrechungen und den letzten Fehlertyp aus. Die HA-Optionen microstructure_enabled (Standard true) und microstructure_max_symbols (Standard 12, begrenzt auf 1–20) steuern die Erfassung.

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

