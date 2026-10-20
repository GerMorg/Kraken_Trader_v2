# Strategie- und Research-Hypothese

## Kandidat v1

Der erste Kandidat ist eine einfache H1-Time-Series-Momentum-/Trendfolge-Regel, kein Machine-Learning-Modell. Sie berechnet 24-Kerzen- und 72-Kerzen-Renditen in Basispunkten sowie 24-/72-Kerzen-Mittelwerte. LONG bzw. SHORT setzt ein gleichgerichtetes Momentum beider Zeitfenster und eine passende Mittelwertbestätigung voraus. Bei unzureichender Historie wird kein Signal erzeugt.

Die Standardschwellen sind 120 bps über 24 Stunden und 180 bps über 72 Stunden. Sie sind vorab festgelegte Forschungsparameter, nicht empirisch optimierte oder als profitabel nachgewiesene Werte. Das interne Signal-Score ist ausdrücklich **keine kalibrierte Wahrscheinlichkeit**. Die Kostenfilter sollen Spread, Ausführungsgebühr, Slippage und Margin-Gebühren vor einer Entscheidung einbeziehen.

## Wissenschaftliche Motivation (Hypothese, kein Profitabilitätsnachweis)

- Deprez & Frömmel (2024), *Are simple technical trading rules profitable in bitcoin markets?*, International Review of Financial Analysis: https://doi.org/10.1016/j.iref.2024.05.003
- *Momentum in the Cryptocurrency Market: A Comprehensive Analysis under Realistic Assumptions* (2026), Review of Asset Pricing Studies: https://doi.org/10.1093/rapstu/raag011
- Bailey et al., *The Probability of Backtest Overfitting*: https://escholarship.org/uc/item/4w1110bb
- Hurst, Ooi & Pedersen, *A Century of Evidence on Trend-Following Investing*: https://www.aqr.com/Insights/Research/Journal-Article/A-Century-of-Evidence-on-Trend-Following-Investing

### Aktueller Evidenzstand (geprüft am 16. Oktober 2026)

Han, Kang & Ryu, *Momentum in the Cryptocurrency Market: A Comprehensive Analysis under Realistic Assumptions*, Review of Asset Pricing Studies, online veröffentlicht am 13. Oktober 2026, DOI 10.1093/rapstu/raag011. Die veröffentlichte Zusammenfassung berichtet starke Evidenz für Time-Series-Momentum, nahezu keine für Cross-Sectional-Momentum und zeigt, dass unter täglicher Mark-to-Market-Bewertung viele Cross-Sectional-Portfolios liquidiert werden; statistisch signifikante Mittelwertrenditen führten dort teilweise zu negativen Gewinnen. Die Autoren betonen fette Verteilungsschwänze und dass der Mittelwert für Profitabilitätstests nicht genügt. Quelle: https://academic.oup.com/raps/advance-article-abstract/doi/10.1093/rapstu/raag011/8914819

Konsequenzen für diese Implementierung:

- Die vorhandene 24-/72-Stunden-Regel bleibt eine zu prüfende Time-Series-Momentum-Hypothese; sie wird nicht als Renditeprognose mit bekannter Trefferwahrscheinlichkeit interpretiert.
- Ein positiver Momentum-Proxy gilt nicht als kalibrierter Netto-Edge. Das unabhängige Risikogate verlangt explizit `edge_calibrated=True`; der Runtime-Pfad übergibt `False`, bis OOS-/Forward-Belege aus dem unveränderten Modell und nach realistischen Kosten vorliegen.
- Bei gehebelten Positionen sind Liquidationspfad, Gap-/Tail-Loss, Margin-Level, Funding-/Rollover-Währung und unvollständige Order-/Ledger-Zuordnung Freigabekriterien, keine optionalen Komfortmetriken.
- Backtest-Ergebnisse benötigen Mehrfachtest-/Overfitting-Kontrollen und einen nicht zur Parametersuche verwendeten finalen Test; eine gute In-Sample-Mittelrendite genügt nicht.

Die Studien betrachten andere Stichproben, Regeln, Zeiträume und Ausführungsannahmen. Sie belegen nicht, dass dieses Modell für dieses Konto oder gehebelte Kraken-Spot-Margin-Trades profitabel ist. Krypto-Returns sind häufig fett-schwänzig und Drawdowns können steil sein; der Mittelwert allein reicht nicht zur Risikobeurteilung.

## Laufzeit-Shadow-Research ab v0.1.39

Die Laufzeit erfasst jetzt unveränderlich zuordenbare Beobachtungen in SQLite (`research_observations`) pro Paar, Stundenkerze, Strategieversion und festgelegten Schwellen. Einmal pro geschlossener Kerze werden LONG, SHORT oder ein gültiges NONE-Signal erfasst, soweit die Historie ausreichend und die letzte geschlossene Kerze zeitlich plausibel ist. Die Beobachtung speichert Entscheidungszeit, Kerzenzeit, Signalmerkmale, Midpreis aus aktuellem Bid/Ask, Entry-Spread und die verfügbare `TradeVolume`-Taker-Gebühr. Ein doppelter Runtime-Aufruf derselben Kerze erzeugt keine zweite Beobachtung.

Nach `expected_holding_hours` wird die nächstpassende bereits gespeicherte geschlossene Stundenkerze ausgewertet. Fehlt die Zielkerze und liegen spätere Kerzen vor, wird das Ergebnis als unvollständig markiert; ein Kurswert wird nicht geraten. Die Berechnung ist ein Forward-Label, kein simuliertes Fill und keine tatsächliche Portfolio-Rendite. Bei Taker-Gebühren wird folgende Schätzung genutzt:

`Spot-Kostenproxy = 2 × Taker-Gebühr + (Entry-Spread + beim Settlement beobachteter Exit-Spread) / 2 + 2 × angenommene Slippage pro Seite`

Die Slippage ist zunächst auf 5 Basispunkte pro Seite festgelegt und als Annahme ausgewiesen. Der Exit-Spread ist nicht historisch mit dem Zielzeitpunkt verknüpft. Deshalb können diese Zahlen Signale vergleichen und grobe Kostenempfindlichkeit zeigen, aber keine belastbare realisierte Nettorendite belegen. Ist die Taker-Gebühr unbekannt, werden nur Brutto-Kursbewegung und deren Unsicherheit erfasst; Net-Proxy bleibt unbekannt.

Margin-Eröffnungs- und Rollover-Gebühren werden bewusst **nicht** mit null angesetzt. `margin_cost_break_even_bps` entspricht dem maximalen kombinierten Margin-Gebührenbudget, das die kostenbereinigte Spot-Proxyrendite gerade auf null reduzieren würde. Tatsächliche Margin-Nettoergebnisse werden nicht berechnet, bis konkrete, quellen- und zeitbezogene Gebührenevidenz vorhanden ist. Die offizielle Kraken-Dokumentation beschreibt die Margin-Raten als veränderlich; für eine bestehende Position ist der beim Orderabschluss festgeschriebene Rollover-Satz maßgeblich. Diese Laufzeitforschung hat derzeit keine historische L2- bzw. exakte Exit-Orderbook-Evidenz.

`SHADOW_RESEARCH_SUMMARY` teilt kostenbewertete Forward-Labels nach eindeutigen Entscheidungszeitpunkten chronologisch in die ersten 60 Prozent, die folgenden 20 Prozent und die letzten 20 Prozent. Derselbe Entscheidungszeitpunkt kann nicht über diese Splits verteilt werden. Mindestens 100 kostenbewertete Forward-Labels sowie mindestens 20 im letzten Holdout-Teil machen den Holdout lediglich **prüfbereit** (`HOLDOUT_AVAILABLE_REVIEW_REQUIRED`); die Stichprobengrenze ist kein statistischer Profitabilitätsnachweis. Überlappende Haltedauern verursachen abhängige Labels. Kein Research-Report setzt `edge_calibrated=True`, promotet Parameter automatisch oder schaltet Live-Handel frei.

Das ist die erste laufende Forward-Evidenzschicht. Sie ersetzt weder den separaten OHLCV-/Orderbuch-Backtest noch einen späteren echten, beaufsichtigten Ausführungsvergleich.

## Laufender historischer Szenario-Backtest ab v0.1.39

Wenn für einen neuen Stundenbalken eine gültige Research-Beobachtung und eine aktuelle Taker-Gebühr vorliegen und das Instrument LONG- und SHORT-Margin unterstützt, führt die App zusätzlich einen begrenzten OHLCV-Szenario-Backtest aus. Er verlangt mindestens 500 lückenlose Stundenkerzen. Fehlende Stundenkerzen oder unbekannte Taker-Gebühren führen nicht zu einer extrapolierten Performancezahl, sondern zu einem übersprungenen bzw. unvollständigen Ergebnis.

Die Kerzen werden streng chronologisch in 60 % Train, 20 % Validation und 20 % Test getrennt. Vier Szenarien variieren das **gesamte angenommene Margin-Eröffnungs- und Rollover-Kostenbudget je Roundtrip** über 5, 10, 20 und 40 Basispunkte. Dieses Budget wird auf eine angenommene Eröffnungsgebühr und die Rollover-Ereignisse verteilt. Es ist keine aktuelle Börsenquote. Handelsgebühr und Spread werden aus den aktuell verfügbaren Kraken-Taker-Daten bzw. dem aktuellen Ticker als Proxys übernommen; für historische Zeitpunkte liegen sie nicht vor.

`OHLCV_BACKTEST_SCENARIO_REPORT` enthält je Szenario Kennzahlen für Train, Validation und Test, darunter Anzahl Trades, Nettogewinn/-erwartungswert, Profit Factor, Drawdown, Tail-Loss und Kostenanteile, soweit der Simulator sie ausgibt. Der bestehende OHLCV-Simulator nutzt nächste-Bar-Open-Ausführung, ein Volumenbeteiligungs-Limit, modellierte Stop-/Zeitausstiege und standardisierte 2x-Hebel-/10-%-Positionsannahmen. Er modelliert weder historische L2-Tiefe noch echte Fills, Warteschlangen, reale Stornos/Latenz oder die tatsächliche Laufzeit-Risikogröße. Die vier Szenarien zeigen Kostenempfindlichkeit, keine historischen echten Gebühren und keine belegte Profitabilität. Jeder Bericht bleibt `SCENARIO_ONLY_NOT_LIVE_VALIDATION`.

## Stand des Simulators

`app/backtest.py` bietet chronologische Train-/Validation-/Test-Splits, den OHLCV-basierten Szenario-Simulator und zusätzlich `replay_trend_strategy_on_order_books`. Der Orderbuch-Replay verwendet zeitgestempelte L2-Snapshots, wählt nur Snapshots am oder vor dem jeweiligen Ereigniszeitpunkt (mit maximalem Datenalter), konsumiert sichtbare Bid-/Ask-Level in Preispriorität und berücksichtigt Limitpreise, pro Level begrenzte Teilnahme, Teilfüllungen, verbleibende Mengen, VWAP, Spread, book-basierte Slippage und Taker-Gebühren. Gebühren werden separat vom durch die tatsächlichen im Buch ausgeführten Preise entstandenen PnL verbucht; sie werden nicht nochmals als Slippage-/Spread-Pauschale addiert. Fehlt das Exit-Buch, ist es veraltet, bleibt eine Position teilweise offen, ist der nicht ausgeführte Rest eines Teilausstiegs/Einstiegs nicht mit passender Storno-Semantik abgebildet oder sind Margin-Kosten nicht belegt, so wird das Ergebnis nicht fälschlich als vollständig realisierter Netto-Trade ausgewiesen. Ein teilweiser Einstieg bricht mit `ENTRY_PARTIAL_REMAINDER_CANCEL_NOT_SIMULATED` ab; eine Position wird bei fehlendem Folgebar auch nicht auf demselben Zeitstempel wie der Einstieg als geschlossen verbucht.

Die Kosten werden jetzt zusätzlich nach Handelsgebühren, Spread, zusätzlichem Depth-/Slippage-Impact und Margin-Opening/Rollover getrennt ausgewiesen. `gross_mid_market_pnl_usd` ist der Mid-Market-Bezug vor Ausführungskosten; `all_in_costs_usd` summiert alle vier Kostenkomponenten. Die Identität `Mid-Market-Brutto-PnL − All-in-Kosten = Netto-PnL` wird in Regressionstests geprüft. `total_costs_usd` bleibt der bisherige, vom Ausführungs-Gross-PnL explizit abgezogene Kostenwert und ist wegen unterschiedlicher Simulator-Konventionen nicht für den Vergleich der vollständigen Ausführungskosten geeignet; dafür ist `all_in_costs_usd` vorgesehen.

Bei Margin-Positionen benötigt der Replay über `margin_cost_evidence_by_entry_timestamp` eine `MarginCostEvidence` mit getrennten Opening- und Rolloverkosten, Quote-Währung, Source-Referenzen, Evidenz-IDs, effektiven Zeitstempeln und Qualitätskennzeichen (`EXCHANGE_VERIFIED`, `HISTORICAL_SIMULATION` oder `RESEARCH_ESTIMATE`). Auch ein Rolloverbetrag von null benötigt eine explizite Evidenzreferenz. Währungskonflikte und nach dem Exit liegende Rolloverkosten werden abgewiesen. Die Felder machen die Herkunft prüfbar, beweisen aber nicht die Authentizität/Komplettheit der externen Quelle; ein als Research-Estimate markierter Betrag bleibt eine Schätzung. Ohne Evidenz bleibt Netto-PnL unbekannt. Die USD-Quote-PnL-Rechnung darf daher einen EWR-USDC-Kredit nicht stillschweigend als USD-Kosten oder als Nullkosten behandeln.

**Wichtige Grenze:** Auch der orderbuchbasierte Replay ist aktuell nur ein deterministisches Szenario-Modell. Im Repository liegt kein qualitätsgesicherter, historischer und versionsgebundener L2-Datensatz vor. Maker-Queue-Priorität, versteckte Liquidität, echte Kraken-Latenz, Cancel/Replace-Rennen, börsenseitige Ablehnungs-/Teilausführungsdetails, historische kontospezifische Gebühren und USDC-/Margin-Ledgerzuordnung sind weiterhin nicht vollständig modelliert bzw. belegt. Es wurde deshalb noch kein gültiger Out-of-Sample- oder Forward-Shadow-Nachweis erbracht. Fehlende Daten sind kein stillschweigender Nullwert und kein Profitabilitätsnachweis.

Die OHLCV-Kennzahlen bleiben als `SCENARIO_ONLY_NOT_LIVE_VALIDATION` ausgewiesen; der neue L2-Replay verwendet `ORDERBOOK_SCENARIO_ONLY` nur bei vollständig abgearbeiteten Szenarien und andernfalls `INCOMPLETE_REQUIRES_REVIEW`. Keine dieser Statusmeldungen gibt Live-Orders frei oder beweist Profitabilität.

## Vorab festgelegter Research-Ablauf

1. Historische OHLCV-, Spread-, Orderbuch- und Instrumentmetadaten mit Version, Quellzeit und Datenlücken dokumentieren.
2. Entwicklung, Validierung und finalen Test streng chronologisch trennen; den finalen Test nicht zur Parametersuche verwenden.
3. Den ausführbaren Simulator um Orderbuch-Tiefe, Fee-Stufen, Limit-/Market-Ausführung, Teilfüllungen, Stornos, Kraken-Mindestgrößen und tatsächliche Margin-/USDC-Finanzierung erweitern.
4. Gegen USD-Cash, Buy-and-Hold, Nullstrategie und wenige vorab benannte Trend-/Momentum-Baselines vergleichen.
5. Netto-Expectancy, Sharpe/Sortino, Drawdown, Erholungszeit, Tail-Loss/Expected Shortfall, Long-/Short-Ergebnisse, Kostenanteil, Turnover, Margin-Auslastung und Stressverlust ausweisen.
6. Walk-Forward und Daten-Leakage-Prüfungen anwenden; bei breiter Varianten-/Parametersuche Mehrfachtesteffekte mit PBO bzw. Deflated Sharpe Ratio behandeln.
7. Strategie/Parameter einfrieren und Shadow-Forward-Test einschließlich Nicht-Trades, Soll-Intents, Live-Marktbedingungen und Wiederherstellungsfehlern protokollieren.
8. Erst nach vorab dokumentierten Stichproben-, Performance- und Sicherheitskriterien separat über einen kleinen beaufsichtigten Live-Test entscheiden.

News, Gemini, autonomes Lernen und automatische Parameterpromotion sind im v1-Umfang ausdrücklich nicht vorgesehen.