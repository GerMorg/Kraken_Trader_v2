## Architekturstatus nach 0.1.75

Die Produkttrennung bleibt verbindlich: Der vorhandene Kraken-Spot-/Spot-Margin-Client und die Spot-Order-Authority werden nicht für Futures wiederverwendet. Der Futures-Produktbereich besitzt weiterhin eigene API-Credentials, Account-/Wallet-/Position-/Order-/Fill-/Funding-Modelle und muss eine eigene Entscheidungs- und Order-Authority erhalten.

Stand 0.1.75 ist der Produktions-Futures-Client weiterhin **Read-only**. Er besitzt keine SendOrder-, AmendOrder- oder CancelOrder-Funktion. Der vorhandene Produktions-Key wurde als `READ_ONLY` erkannt und reicht nicht zur Orderausführung. Dieser Zustand darf nicht als fertig integrierter Futures-Adapter bezeichnet werden.

Der nächste Futures-Schritt ist ein eigener, demo-first Trading-Adapter mit exakter POST-Signaturvalidierung, dedizierten Order-IDs, Fill-Bestätigung und separater Reduce-only-Trailing-Stop-Order. Erst nach Demo-Tests für Partial Fills, Stop-Amend/Cancel, Restart-Recovery, Position-/Order-Reconciliation, Gebühren/Funding und Schutzorder-Ausfall darf ein Produktionsadapter mit einem separaten Trading-Key ohne Transfer-/Withdraw-Rechte geprüft werden. Der allgemeine Operator-Schalter `real_orders_enabled` genügt nicht, um diese Produkt- und Release-Gates zu umgehen.

# Kraken Derivatives EEA – Research- und Integrationsspezifikation

**Beschlussdatum:** 21.11.2026  
**Repository-Ausgangspunkt:** main, App-Version 0.1.67, Commit 1c82ea59e1074cf8432cd8beadd343ed449e941e  
**Status:** Architekturentscheid / noch nicht implementiert. Keine Live-Freigabe.

## 1. Ziel und klare Produktgrenze

Die bestehende Anwendung unterstützt Kraken Spot und Spot Margin. Diese Pfade bleiben bestehen und werden nicht durch Futures ersetzt. Kraken Derivatives EEA wird als eigenständige Produktstrecke ergänzt, weil sie eine getrennte API, andere Authentifizierung, ein eigenes Konto-/Kollateralmodell, andere Positions- und Margin-Daten sowie Funding- und Liquidationsmechanismen besitzt.

Die Erweiterung darf nicht als weiterer product_type-Wert in die bestehende Spot-Orderausführung hineingesteckt werden. Insbesondere dürfen folgende Objekte nicht ungeprüft wiederverwendet werden:
- Spot KrakenClient, Spot-Signatur/Nonce-Verfahren und Spot-Endpunkte;
- Spot Instrument, Margin-Leverage-Stufen und RiskSnapshot;
- Spot-Order- und Margin-Settlement-Zustände;
- Spot-Balance, Wallet-Provenienz, Kredit- und USDC-Konversionsannahmen;
- Spot-Fees, Margin-Opening-/Rollover-Kosten und PnL-Berechnung.

Semantisch identische primitive Hilfsfunktionen wie Decimal-Prüfungen oder sichere HTTP-Fehlerbehandlung dürfen gemeinsam genutzt werden, wenn dies durch Tests belegt ist.

## 2. Kontozugang und EWR-Regeln

Für Privatkunden verlangt Kraken zur Freischaltung von Derivatives ein vollständig verifiziertes Kraken-Konto. Für neue EWR-Derivatives-Kunden kommen ein Angemessenheitsfragebogen und die Steueridentifikationsnummer (TIN) hinzu. Die konkrete Berechtigung und das freigeschaltete Produktuniversum sind im tatsächlichen österreichischen Konto zu prüfen; der Quellcode darf aus einer allgemeinen Länder- oder Produktliste keine Konto-Freigabe ableiten.

Die erste Implementierung beschränkt sich auf lineare Multi-Collateral-Krypto-Perpetuals mit USD-notierten Kontrakten. Inverse Kontrakte, Fixed-Maturity-Kontrakte, xStocks, tokenisierte Aktien, Indizes und Rohstoffreferenzen bleiben zunächst außerhalb des ersten Piloten. Eine spätere Erweiterung auf zusätzliche Produktarten benötigt eigene Kontrakt-, Handelszeiten-, Index-, Fee-, Funding- und Settlement-Tests.

### EUR und Multi-Collateral

Kraken listet EUR im EEA-Collateral-Dokument vom 30.10.2026 mit 0 % Haircut und 0,0 % Conversion Fee. Das ist keine Aussage, dass Trading, PnL, Funding oder andere Währungsumwandlungen kostenlos sind:
- Multi-Collateral-Linear-Derivate führen Fees in USD.
- Sind USD für Fees, PnL oder Funding-Auszahlungen nicht vorhanden, kann Kraken verfügbare Nicht-USD-Kollateralwerte konvertieren.
- Ungedeckte Verluste können gemäß der jeweils geltenden Gebühren-/Zinsregel belastet werden.
- Jede tatsächliche Kontobewegung ist dem Futures-Wallet-Ledger zu entnehmen; EUR-Kollateralwert, USD-PnL und USD-Fee dürfen nicht vermischt werden.

Kein Spot-Saldo gilt automatisch als Futures-Sicherheit. Für den ersten Echtgeldversuch wird ein manueller Transfer in das Derivatives-Wallet durch den Nutzer vorausgesetzt. Die App erhält keine Transfer- oder Auszahlungsberechtigung und führt keine automatischen Ein-/Auszahlungen durch.

### Funding und Kosten

Bei EEA-Perpetuals wird Funding während der Position fortlaufend berechnet und stündlich verrechnet. Die Strategie muss die erwartete Haltedauer, historische und aktuelle Funding-Raten sowie die tatsächliche kumulierte Funding-Buchung berücksichtigen. Ein Funding-Ertrag kann positiv oder negativ sein; eine einzelne aktuelle Rate darf nicht unreflektiert über die gesamte Haltedauer fortgeschrieben werden.

Der EEA-Fee-Satz ist volumenabhängig und getrennt vom Spot-Fee-Tier. Spot-Volumen wird nicht auf Futures-Fee-Volumen angerechnet. Die Runtime darf keine dauerhaft fest codierten Maker-/Taker-Sätze verwenden. Tatsächliche Fill-Fees und Account-Ledger-Buchungen haben Vorrang.

## 3. Offizielle API-Verträge

### Hosts

- Produktion REST: https://futures.kraken.com/derivatives/api/v3
- Produktion WebSocket: wss://futures.kraken.com/ws/v1
- Demo REST: https://demo-futures.kraken.com/derivatives/api/v3
- Demo WebSocket: wss://demo-futures.kraken.com/ws/v1

Die Demo benötigt eigene Demo-Anmeldedaten/API-Schlüssel. Kraken beschreibt die REST-/WebSocket-Feeds und Response-Strukturen als mit Produktion identisch; nur der Host unterscheidet sich. Zugangsdaten und URLs müssen fest auf eine explizite Umgebung begrenzt sein. Ein vom Benutzer beliebig gesetzter API-Host ist aus Sicherheitsgründen nicht zulässig.

### Authentifizierung

Futures verwendet das Headerpaar APIKey und Authent und ein eigenes Signaturverfahren. Ein Nonce-Header ist laut Kraken-REST-Dokumentation optional; in der ersten Read-only-Stufe wird er bewusst nicht gesendet, damit keine ungeprüfte Nonce-Persistenz eingeführt wird. Nach dem offiziellen Kraken-/Crypto-Facilities-REST-v3-Beispiel wird der tatsächlich serialisierte Query-/POST-Datenstring mit Nonce und Endpoint-Pfad kombiniert, SHA-256-gehasht, mit dem Base64-dekodierten Secret via HMAC-SHA-512 signiert und das Ergebnis Base64-kodiert. Der /derivatives-Präfix wird bei der Signaturpfadbildung entfernt.

Die exakte Serialisierung muss gegen die aktuelle offizielle Implementierung und reproduzierbare Testvektoren validiert werden. GET-Querystring, POST-Querystring, Nonce-Header und signierter String müssen bytegenau zusammenpassen. Spot-Signaturen dürfen nicht adaptiert oder wiederverwendet werden. Es werden getrennte Futures-API-Schlüssel benötigt. Keine Abhebungs- oder Transferrechte.

### Relevante Endpunkte (mindestens)

| Zweck | Futures REST API |
|---|---|
| Kontraktuniversum | GET /instruments |
| Ticker / Orderbuch | GET /tickers, GET /orderbook |
| Multi-Collateral-Wallets | GET /accounts (Wallet-/Accountdaten im Response-Objekt) |
| Offene Positionen | GET /openpositions |
| Offene Orders | GET /openorders |
| Fills / Ausführungen | GET /fills |
| Historische Funding-Raten | GET /historical-funding-rates?symbol=... |
| Orders | POST /sendorder, /editorder, /cancelorder |
| Stoppen neuer/offener Orders | POST /cancelallordersafter (Dead Man's Switch) |
| Account-Leverage-Präferenzen | GET /leveragepreferences |

Vor Implementierung jeder weiteren Funktion sind Endpoint, Parameter, Rechte, Antwortschema und Rate-Limit erneut gegen die aktuelle API-Referenz zu prüfen.

### Orders sind nicht Fills

Die Futures-API kann result=success zurückgeben, obwohl sendStatus.status beispielsweise insufficientAvailableFunds lautet. Ein erfolgreicher HTTP-/API-Aufruf ist daher nicht gleichbedeutend mit einer platzierten Order oder einem Fill. Die Anwendung muss sendStatus.status, Exchange-Order-ID, Client-Order-ID, Open Orders, Fills und tatsächliche Positionsänderung unabhängig abgleichen.

Für Client-Order-IDs verlangt Kraken globale Eindeutigkeit. Die Anwendung muss eine persistente und kollisionssichere ID erzeugen. Nach Timeout, Verbindungsabbruch oder mehrdeutiger Antwort bleibt der Zustand UNKNOWN_RECONCILING; es erfolgt kein blinder Wiederholungsauftrag.

Jede reine Positionsreduktion und jede Schutzorder muss mit reduceOnly=true übermittelt werden, sofern der aktuelle Endpunktvertrag dies unterstützt. Stop- und Take-Profit-Trigger müssen die Triggerquelle (mark, index oder last) explizit dokumentieren. Bevorzugte Risikoreferenz ist zunächst Mark Price; die endgültige Wahl muss mit Liquidations-/Indexmethodik und Demo-Verhalten validiert werden.

## 4. Dynamische Instrument- und Margin-Erkennung

Das Instrumentuniversum wird ausschließlich aus dem aktuellen Futures-Instrument-Endpoint aufgebaut. Keine Whitelist auf Basis von Symbolnamen oder früheren Marktlisten. Pro Instrument sind mindestens zu speichern und regelmäßig zu erneuern:
- Symbol, Produkt-/Kontrakttyp, Basis-/Quote- und Settlement-Währung;
- tradeable, Ablauf-/Settlement-Zustand und Plattform-/Länderrestriktionen;
- Mindestmenge, Mengenpräzision, Tickgröße, Contract Size und maximale Positionsgröße;
- Margin-Klasse sowie alle Positionsgrößenstaffeln für Initial- und Maintenance-Margin;
- Retail-Margin-Level, soweit von der aktuellen API geliefert;
- Funding-Multiplikator, Funding-Grenzen, Index-/Mark-Preisparameter;
- Ordertypen, Post-only-Verfügbarkeit, Status und aktuelle Kontraktänderungen.

Die öffentlich beschriebene EEA-Margin-Tabelle enthält Kontraktklassen und kann bis zu 10x maximale Initial-Hebelstufe ausweisen. Das ist keine Freigabe, jedem Konto oder Instrument 10x zuzuweisen. Das effektive Konto-/Retail-Limit kann niedriger sein und muss aus aktuellen Instrument- und Kontoantworten abgeleitet werden. retailMarginLevels, Margin-Staffel, Kontoeinstellungen und verfügbare Kollateralwerte sind vor jeder Risikoerhöhung zu prüfen.

Leverage-Präferenzen werden in der ersten Implementierungsphase ausschließlich gelesen. Keine automatische Änderung von Konto-Leverage- oder Margin-Einstellungen.

## 5. Risikopolitik und stufenweise Hebelfreigabe

Hebel bleibt ein vorgesehenes und später ausbaubares Merkmal. Er wird jedoch nicht mit einem höheren Prognosevorteil verwechselt: Hebel skaliert Gewinn und Verlust relativ zum eingesetzten Eigenkapital und kann Liquidationsrisiko und Kosten beschleunigen.

### Positionsgröße

Die maximale Positionsgröße wird aus dem kleinsten anwendbaren Limit gebildet:

Notional = min(Risikobudget / (Stop-Abstand + Gap-Puffer + Roundtrip-Kosten + erwartetes Funding), Eigenkapital × interner Hebelcap, verfügbare Margin / Initial-Margin-Satz, Instrument-Maximum)

Alle Terme müssen dieselbe Prozent-/Währungsbasis haben. Fehlende Stop-, Fee-, Funding-, Index-, Margin- oder Kollateralwerte führen zu INCOMPLETE und verhindern neue Risikoerhöhung. Die tatsächlich zulässige Kontraktmenge wird konservativ auf Lot- und Mengenpräzision abgerundet. Niemals zur Mindestmenge aufrunden.

### Anfangsgrenzen

Für den späteren Echtgeld-Pilot sind vorgesehen:
- Experimentbudget insgesamt: 250 EUR; nur 100 EUR erste Live-Tranche, 150 EUR bleiben außerhalb des Futures-Wallets.
- eine offene Position gleichzeitig;
- maximal 1 % des aktuellen Eigenkapitals geplantes Verlustbudget pro Trade einschließlich Kosten- und Gap-Puffer;
- anfänglicher interner effektiver Hebelcap: 1,5x;
- Isolated Margin, sofern für den gewählten Vertrag und das EEA-Konto tatsächlich verfügbar;
- keine Nachschüsse, keine Martingale-Logik, keine automatischen Wallet-Transfers;
- Tagesverlust- und Drawdown-Limits sowie globaler Futures-Kill-Switch als code-seitige Höchstgrenzen.

Der Cap von 1,5x ist ein anfänglicher Pilotwert, nicht die endgültige Architekturgrenze. Später kann die Anwendung höhere Stufen bis maximal zu den dann tatsächlichen Retail-/Instrument-Limits und der internen Code-Freigabe ermöglichen. Eine Erhöhung benötigt eine bewusste Codeänderung, neue Stress-/Liquidationstests, ausreichende reale Nettoergebnisse und eine ausdrückliche Freigabe; keine automatische Skalierung nach Gewinnen.

Vor einem höheren Hebel sind mindestens tatsächliche Netto-Trade-Ergebnisse, adverse Slippage, Funding-Kosten, Gap-Stress, Margin-Staffelung und Stop-/Liquidationsabstände separat nachzuweisen. Ein 10x-Kontraktlimit ist niemals das Default-Ziel.

## 6. Börsenseitiger Schutz und Dead Man's Switch

Jede Live-Position muss nach dem Einstieg eine serverseitig aktive, zur Position passende Stop-Order erhalten. Die Schutzorder muss nach Submit mit Exchange-ID, Symbol, Seite, Reduce-only-Flag, Stop-Preis, Triggerquelle und geschützter Restmenge wieder eingelesen und verifiziert werden. Teilfüllungen erfordern eine mengenexakte Anpassung der Schutzorder. Ein Exit ist erst dann bestätigt, wenn Fills und Open Positions konsistent sind.

Kraken empfiehlt für den Futures-Dead-Man's-Switch regelmäßige Verlängerung (alle 15–20 Sekunden mit 60 Sekunden Timeout). Dieser Mechanismus storniert offene Orders, wenn der Prozess ausfällt; er liquidiert keine offenen Positionen und ersetzt daher keinen positionsbezogenen Stop. Die Runtime muss Stop-Status und Dead-Man's-Switch getrennt protokollieren.

Bei Datenstillstand, Netzwerkunterbrechung, API-Fehlern, inkonsistentem Konto, unbekanntem Liquidations-/Marginstatus oder fehlendem Schutz darf keine neue Risikoerhöhung stattfinden. Bereits offene Positionen werden weiter überwacht bzw. risiko-reduzierend behandelt, soweit der aktuelle Exchange-Zustand verifiziert und die API verfügbar ist. Der Kill-Switch darf das Schließen oder Reduzieren einer bestehenden Position nicht versehentlich blockieren.

## 7. Persistence, Reconciliation und Steuerbuchhaltung

Futures erhält eigene Produktidentitäten und persistente Datensätze für mindestens:
- Account-/Wallet-Snapshots und alle Kollateralwährungen samt USD-Bewertung, Haircut, Conversion- und Zeitquelle;
- Instrument-Metadaten und versionierte Margin-/Funding-Parameter;
- Entscheidungen/Intents, Order-IDs und Client-Order-IDs, jede Order-State-Änderung;
- einzelne Fills mit Preis, Menge, Richtung, Fee, Fee-Currency, Maker/Taker, Timestamp und Referenz;
- offene/geschlossene Positionen, Mark Price, Unrealized PnL, Funding und Marginverbrauch;
- Funding-Sätze, erwartete Funding-Schätzung und tatsächliche Funding-Abbuchung/-Gutschrift;
- Stops, Take-Profits, Trigger, Reduce-only, zugeordnete Positionsmenge und Verifikationszeitpunkt;
- Account-Ledger/Transfers/Settlements/Liquidationen und EUR-Steuerbewertung.

Die Reconciliation vergleicht Wallets, Open Positions, Open Orders, Fills und Account-History/Log. Der lokale Zustand gilt nur dann als vollständig, wenn alle Zeitfenster und Seiten-Cursor vollständig und ohne unerklärte Differenzen synchronisiert sind. Externe manuelle Trades, Wallettransfers und Liquidationen werden explizit als externe oder ungeklärte Ereignisse erkannt. Bei ungelösten Abweichungen sind neue Entries gesperrt.

Neue Tabellen-/Schemaänderungen sind additiv, versioniert, transaktional und idempotent. Kein Zurücksetzen, Löschen oder Neuerstellen der bestehenden Spot-Datenbank. Der Steuerexport muss Futures getrennt als Derivate erfassen; keine pauschale Übernahme der bisherigen Spot-/Margin-Steuerklassifizierung. Die konkrete österreichische steuerliche Einordnung und EUR-Bewertung sind gesondert zu prüfen.

## 8. Research-, Demo- und Live-Freigabe

### Research

- Nur lineare EEA-Krypto-Perpetuals im ersten Schritt.
- Eine vorab dokumentierte Trend-/TSM-Baseline plus höchstens eine Vergleichsvariante.
- Chronologische Train-/Validation-/unangetastete Testabschnitte; keine Parameterpromotion aus demselben Testfenster.
- Kostenmodell mit Maker-/Taker-Fee, Spread, Slippage, Funding, EUR/USD-Kollateralbewertung, Conversion/Interest wo tatsächlich anfallend, Market-/Mark-Price-Differenzen, Teil-/Nichtfills und Mindestmengen.
- Benchmark gegen Cash und ungehebelte Positionssimulation.
- Long und Short getrennt auswerten.
- Kennzahlen: Netto-PnL, Trade-Expectancy, Profit Factor, Sharpe/Sortino, maximaler Drawdown, Expected Shortfall, Verlustserien, Turnover, Funding-/Fee-Anteil, Liquidations-/Stop-Stress und Margin-Auslastung.
- Unbekannte oder nicht historische Kosten werden als unbekannt ausgewiesen und nicht auf null gesetzt.

Die bisherige Spot-TSM-/Parrondo-Forschung ist keine Futures-Performance-Evidenz. Keine bestehende Strategie wird ohne Futures-spezifischen Test als profitabel übernommen.

### Demo

- Demo-API-Key und Demo-Host strikt getrennt von Produktion.
- Mindestens 14 aufeinanderfolgende Kalendertage und mindestens 20 abgeschlossene Demo-Trades als operative Basisschwelle; bei zu geringer Handelsaktivität wird die Periode verlängert oder die Strategie neu bewertet, nicht künstlich mehr gehandelt.
- Tests für Long, Short, Limit, Post-only, Stop, Reduce-only, Teilfill, Cancel, Timeout/unknown und Neustart-/Reconciliation.
- Nach jedem Testlauf müssen Orderhistorie, Fills, Positionen, Funding, Wallet und Schutzorders übereinstimmen.
- Die Demo ist kein Nachweis echter Marktwirkung, Liquidität, Queue-Position, Slippage oder tatsächlicher Live-Kosten.

### Live-Pilot

Live bleibt unabhängig vom Spot-Live-Schalter und standardmäßig code-seitig gesperrt. Vor Freigabe müssen API-/Kontoberechtigung, Demo, Instrumente, Risiko, vollständige Reconciliation, serverseitige Stops, Neustart-Recovery, Logging, Steuerdaten und alle CI-Jobs nachgewiesen sein. Der erste Live-Pilot beginnt erst danach mit 100 EUR und höchstens 1,5x effektivem Hebel. Das übrige Experimentbudget bleibt außerhalb.

## 9. Testanforderungen (keine Ausnahmen)

Regressionstests müssen mindestens abdecken:
1. Futures-Signatur: bekannte Testvektoren, getrennte Spot/Futures-Signaturen, exakte URL-Encoding-Übereinstimmung, Nonce-Kollisionen und Secret-Redaction.
2. Environment: kein Produktionsrequest aus Demo, kein Demo-Schlüssel auf Live-Host, keine vom Benutzer frei definierbare Host-URL.
3. Instrumente: ungültige/metadatenarme/inaktive/abgelaufene Märkte, Nicht-USD-Quote, inverse vs. lineare Kontrakte, Max-Position, Mindestmenge, Tick-/Lot-Präzision, geänderte Margin-Klassen.
4. EEA-Retail: fehlende Retail-Margin-Level, Margin-Staffeln/Notional-Schwellen, Cross/Isolated, geänderte Account-Leverage-Präferenzen, Haircuts und fehlende USD-/EUR-Bewertung.
5. Orderstatus: result=success mit status=insufficientAvailableFunds, Platzierung ohne Fill, Teilfill, verspätete Fillmeldung, Reject, Cancel, Duplicate Client-ID, Timeouts und unbekannter Submitzustand.
6. Richtung/Mengen: Long/Short-Side, exakt positionserhöhende und reduce-only Orders, Mengenabrundung statt Aufrundung, kein Cross-Market-/Cross-Product-Lotverbrauch.
7. Schutz: Stop nach Einstieg, Partial-Fill-Anpassung, Mark-/Index-Trigger, falsche Seite/Menge, Stop-Fehlen, Neustart und Dead-Man's-Switch-Ablauf; DMS darf nicht als Positionsschutz gewertet werden.
8. Funding/Fees: positive/negative Raten, stündliche Settlement-Zeitpunkte, geänderte Raten, tatsächliche vs. geschätzte Buchungen, doppelte Ledger-Einträge und fehlende Währungskonvertierung.
9. Recovery/Reconciliation: Restart nach Submit/Ack/Fill/Cancel, Wallet-/Positionsabweichungen, externe Trades, manuelle Transfers, fehlende Historienseiten und API-Limitfehler.
10. Risk: Gap durch Stop, Mark-/Index-/Last-Differenzen, plötzliche Margin-Klassenänderung, Funding-Sprung, Collateral-Haircut-Änderung, Liquidationsnähe, Portfolio- und Tagesverlustgrenzen.

Die aktuelle CI mit Quality (Ruff/Mypy/pytest), Security (Bandit/pip-audit), AppArmor, HA-Struktur, Python-Compile und Container-Smoke-Tests bleibt Pflicht. Ein Futures-PR wird nur anhand des finalen PR-Head-Commits bewertet; nicht anhand eines früheren grünen Zwischenstands.

## 10. Offizielle Referenzen (abgerufen 21.11.2026)

- Kraken Derivatives EEA eligibility: https://support.kraken.com/at/articles/360023786632-kraken-derivatives-eligibility
- Unlock Derivatives for EEA clients: https://support.kraken.com/articles/unlock-derivatives-eea
- Futures API URLs: https://support.kraken.com/articles/360022839491-api-urls-derivatives
- Futures REST calls and returns: https://support.kraken.com/articles/360022635572-calls-and-returns-rest-api-derivatives
- Futures API instruments: https://docs.kraken.com/api-reference/instrument-details/get-instruments
- Futures API send order: https://docs.kraken.com/api-reference/order-management/send-order
- Futures API open positions: https://docs.kraken.com/api-reference/account-information/get-open-positions
- Futures API get wallets: https://docs.kraken.com/api-reference/account-information/get-wallets
- Futures API leverage preferences: https://docs.kraken.com/api-reference/multi-collateral/get-leverage-settings
- Historical funding rates: https://docs.kraken.com/api-reference/historical-funding-rates/historical-funding-rates
- Dead Man's Switch: https://docs.kraken.com/api-reference/order-management/dead-mans-switch
- EEA margin schedule: https://support.kraken.com/at/articles/derivatives-margin-schedule-and-maximum-leverage-eea
- EEA linear multi-collateral contract specs: https://support.kraken.com/gb/articles/perpetual-contract-specifications-for-clients-in-the-eea
- EEA collateral currencies: https://support.kraken.com/au/articles/collateral-currencies-eea
- EEA Derivatives fees: https://support.kraken.com/in/articles/fees-for-derivatives-trading-eea
- Futures API demo environment: https://support.kraken.com/articles/360024809011-api-testing-environment-derivatives

Produkt-, Margin-, Gebühren- und Länderbedingungen können sich ändern. Die Werte in dieser Spezifikation sind keine dauerhaften Konstanten. Bei Implementierung und jedem späteren Produktfreigabe-Check müssen die aktuellen Kraken-Antworten und offiziellen Bedingungen erneut geprüft werden.

## 11. Implementierungsstand Read-only API

Version 0.1.68 lieferte einen eigenen Futures-REST-Client für die isolierte Demo. Version 0.1.69 ergänzt einen strikt getrennten Produktions-Read-only-Modus für einen Kraken-Futures-Key mit ausschließlich lesenden Exchange-Rechten. Die beiden Hosts sind fest codiert; ein frei konfigurierbarer Host ist nicht möglich. Die Methoden-Allowlist bleibt auf GET-Instrumente, Ticker, Accounts, offene Positionen/Orders, Fills, Leverage-Präferenzen und historische Funding-Raten begrenzt. Es existieren weiterhin keine Order-, Cancel-, Transfer- oder Account-Änderungsmethoden. Die HA-Konfiguration führt Futures-Key/Secret getrennt von Spot-Credentials und zeigt in Diagnosen ausschließlich an, ob beide Werte vorhanden sind. Der Schalter `futures_readonly_enabled` ist standardmäßig aus. Die Konfigurationsfelder allein starten noch keinen Produktions-Sync; die Runtime-Anbindung, persistente Futures-Snapshots und Reconciliation folgen als eigener Schritt. Ein Futures-Key darf nur mit den minimal erforderlichen Leserechten und ohne Trade-/Transfer-/Withdrawal-Rechte hinterlegt werden.


## 12. Runtime Read-only Sync — Version 0.1.70

Version 0.1.70 verbindet den Produktions-Read-only-Client mit dem HA-Runtime-Zyklus. Die Futures-Zugangsdaten bleiben getrennt von Spot. Der Schalter `futures_readonly_enabled` bleibt standardmäßig deaktiviert; das Vorhandensein der Credentials allein startet keine Abfrage.

Vor dem ersten Kontenrequest ruft die Anwendung `GET /api/auth/v1/api-keys/v3/check` auf und prüft die vom Exchange zurückgegebenen Rechte. `general` muss exakt `READ_ONLY` sein; `transfer` darf `NO_ACCESS` oder `READ_ONLY` sein, niemals `FULL_ACCESS`. Die zurückgegebene API-Key-ID wird nur intern verglichen; API-Key, Account-UID, IIBAN und CIDR-Werte werden weder im AppLog noch im Futures-Snapshot gespeichert. Bei nicht passenden oder unvollständigen Rechten werden keine privaten Kontenabfragen ausgeführt.

Nach erfolgreicher Rechteprüfung liest die Runtime getrennt:
- Accounts / Multi-Collateral-Wallets;
- offene Positionen, offene Orders, Fills und Leverage-Präferenzen;
- Instrument-Metadaten und Ticker.

Jeder erfolgreiche Account-Read-Zyklus wird mit Zeitstempel und Endpunktstatus in `futures_readonly_snapshots` append-only gespeichert. Rohantworten verbleiben ausschließlich im separaten Futures-Datensatz; sie werden nicht in Spot-Balance-, Fill-, Order- oder Tax-Tabellen kopiert. Das AppLog erhält nur Rechte-Tiers, Status, Snapshot-ID, Endpunkt-Erfolgsstatus und Datensatzanzahlen. Unvollständige Reads werden als PARTIAL gespeichert; fehlende Antworten werden nicht als leere Positionen oder leeres Konto interpretiert. Ein Fehler im Futures-Pfad darf den bestehenden Spot-Zyklus nicht automatisch in einen Futures-Orderpfad umleiten oder dessen Live-Freigaben beeinflussen.

Der Stand ist weiterhin **Read-only**: Es existieren keine Futures-Sendorder-, Cancel-, Transfer-, Leverage-Änderungs- oder Stop-Änderungsfunktionen. Der initiale Snapshot ist noch keine vollständige historische Futures-Reconciliation. Funding-Historie, Account-Ledger-Paginierung, EUR-Bewertung, Futures-Steuerereignisse und unabhängige Wallet-/Fill-/Positionsabgleiche folgen in eigenen Entwicklungsstufen.

Offizielle Referenz für den API-Key-Check: https://docs.kraken.com/api/docs/futures-api/auth/check-v-3-api-key


## 13. Account-Response-Auswertung — Version 0.1.71

Die Futures-REST-Antwort `GET /accounts` liefert unter `accounts` eine Map mit getrennten Wallet-Klassen. Der Multi-Collateral-Account ist in der aktuellen REST-Struktur der Eintrag `flex` mit `type=multiCollateralMarginAccount`; dessen unterstützte/gelistete Kollateralwährungen stehen unter `flex.currencies`. Der `cash`-Eintrag ist ein Holding-/Cash-Wallet mit `cash.balances`; Single-Collateral-Konten sind eigene Einträge vom Typ `marginAccount`.

Die frühere Auswertung suchte irrtümlich nach `accounts.multiCollateral.balances`. Das konnte einen Null-Zähler liefern, obwohl die REST-Antwort ein Flex-Wallet enthielt. Version 0.1.71 wertet deshalb die REST-Walletstruktur aus und weist folgende Dinge getrennt aus:
- Anzahl und Typen der von Kraken gelieferten Wallet-Einträge;
- Flex-Kollateralwährungen sowie davon Währungen mit positiver Menge;
- Flex-Währungsmenge, USD-Wert, Kollateralwert, verfügbare Margin, Haircut und Conversion Spread, soweit Kraken diese Felder liefert;
- Flex-Account-Metriken wie Balance Value, Portfolio Value, Collateral Value, Margin Equity und Available Margin;
- Cash-/Holding-Währungen und deren Salden;
- Single-Collateral-Wallets und deren Balance-Einträge.

Cash-/Holding-Salden und Single-Collateral-Konten werden nicht in die Flex-Kollateralwerte eingerechnet. Ein gelisteter Währungseintrag ist außerdem nicht automatisch ein finanzierter Saldo: `collateral_currency_count` zählt die Einträge, `funded_collateral_currency_count` nur Einträge mit positiver Quantity. Ein fehlender oder leerer Flex-Account wird ausdrücklich als eigener Schema-Status gemeldet; er wird nicht als erfolgreicher Kontostand von null interpretiert.

Die Originalantwort bleibt im isolierten Futures-Snapshot gespeichert. Das AppLog zeigt nur die strukturierten Wallet-/Margin-Diagnosen und keine API-Keys, Account-UIDs oder IIBAN-Daten.

Referenz für die REST-Walletbeispiele: https://python-kraken-sdk.readthedocs.io/en/stable/05_futures/rest.html
