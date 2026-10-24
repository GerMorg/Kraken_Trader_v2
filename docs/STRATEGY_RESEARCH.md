# Strategie- und Research-Hypothese

## Validierungshinweis ab App-Version 0.1.43

Der OHLCV-Szenario-Backtest prüft Stops ausschließlich in Kerzen, die vor dem geplanten Time-Exit-Balken liegen. Der Time-Exit findet am Open des geplanten Exit-Balkens statt; dessen späteres High/Low wird nicht ausgewertet. Ein Take-Profit reduziert einmalig den konfigurierten Anteil der verbleibenden Position (Standard 50 % bei +3,5 % Brutto-Kursbewegung), während die Restposition bis Stop oder Time-Exit verwaltet wird. Werden Stop und Take-Profit innerhalb derselben Stundenkerze berührt, nimmt der Simulator konservativ an, dass der Stop zuerst ausgelöst wurde. OHLC enthält keine genaue Intrabar-Reihenfolge; auch diese Regeln bleiben daher Annahmen und kein Fill-Nachweis.

Diese Korrektur verhindert Look-ahead am geplanten Exit-Balken und macht die Simulation nicht vollständig realitätsgetreu: Es fehlen weiterhin echte historische Orderbuchdaten über den ganzen Zeitraum, Queue-Position, Latenz, Abbruch-/Ersetzungsrennen, Kraken-spezifische Ausführungsantworten und positionsbezogene echte Margin-Gebühren. Bereits bestehende Forward-Labels bleiben unverändert. Strategieparameter bleiben tsm-0.2.0; eine positive Strategievalidierung ist damit nicht erreicht.


## Validierungshinweis ab App-Version 0.1.42

Die bestehende Forward-Auswertung am Schluss der Stundenkerze bleibt unverändert. Zusätzlich speichert Version 0.1.42 den Open-Kurs jener gespeicherten Stundenkerze, deren Intervall den geplanten Zielzeitpunkt strikt enthält, und berechnet daraus einen separaten Brutto-Forward-Proxy für LONG und SHORT. Das reduziert die systematische Verzögerung durch das Warten auf den nächsten Stundenkerzenschluss, ist aber kein exakter Kurs am Zielzeitpunkt und kein garantiert ausführbarer Preis. Der neue Proxy wird separat chronologisch ausgewiesen und darf nicht als Nettorendite, Fill oder Portfolio-PnL interpretiert werden.

Bereits abgeschlossene Beobachtungen mit gespeicherten Zielkerzen erhalten den neuen Vergleichswert durch einen idempotenten Backfill. Dabei bleiben die bisherigen Brutto-/Netto- und Kostenfelder unverändert. SHADOW_RESEARCH_TARGET_OPEN_BACKFILL erscheint nur, wenn tatsächlich Datensätze ergänzt wurden. Bei fehlender Referenzkerze wird keine Preisreferenz erfunden. Die Strategieversion bleibt tsm-0.2.0, da sich die Signalregel nicht ändert.


## Validierungshinweis ab App-Version 0.1.41

Die Forward-Forschung gibt Brutto-Kursbewegung und kostenbereinigten Spot-Proxy jetzt getrennt aus. So kann erkannt werden, ob negative Resultate bereits aus dem Richtungssignal oder zusätzlich aus Handelskosten entstehen. Außerdem werden der tatsächliche Zeitabstand zwischen Entscheidung und ausgewertetem Stundenkerzenschluss sowie dessen Abweichung vom geplanten Horizont gemessen.

Die Forward-Kennzahlen bleiben **keine Strategie-PnL-Messung**: Sie simulieren nicht den Intrabar-Stop-Loss, das Take-Profit bei 3,5 %, Teilreduktionen, die reale Orderausführung oder Gebühren aus tatsächlichen Fills. Ein negatives Forward-Ergebnis ist daher ein starkes Warnsignal zur weiteren Untersuchung, aber nicht exakt gleichbedeutend mit dem hypothetischen Ergebnis des vollständigen Exit-Zustandsautomaten. Umgekehrt ist es kein Grund, die Regel mit denselben Daten so lange zu verändern, bis die Stichprobe besser aussieht.

## Kandidat v2 — H1-Time-Series-Momentum mit Trendeffizienz (`tsm-0.2.0`)

Die Laufzeitregel bleibt ein einfacher, reproduzierbarer Preis-Trendkandidat. Sie berechnet 24- und 72-Stunden-Momentum in Basispunkten und verlangt für LONG/SHORT eine übereinstimmende Richtung sowie passende 24-/72-Kerzen-Mittelwerte. Version `tsm-0.2.0` ergänzt ein Efficiency-Ratio-Filter: absolute Netto-Bewegung geteilt durch die Summe der absoluten Ein-Kerzen-Bewegungen. Ein hoher Wert bedeutet einen direkteren Preisweg; ein niedriger Wert zeigt, dass das Momentum durch häufige Richtungswechsel zustande kommt.

Die vorab festgelegten Mindestwerte sind 0,20 über 24 Stunden und 0,12 über 72 Stunden. Sie wurden nicht aus den bisherigen Shadow-Ergebnissen optimiert. Sie sind eine zu prüfende Hypothese zur Verringerung von Signalen in seitwärts-/rauschgeprägten Phasen. Schwellen bleiben 120 Basispunkte/24 Stunden und 180 Basispunkte/72 Stunden; Stops basieren weiterhin auf realisierter Kurzzeitvolatilität und werden auf 1,5–5,0 Prozent begrenzt.

Der neue `trend_strength_score` kombiniert die Momentum-Intensität mit den beiden Effizienzmaßen. Er ist nur ein begrenzter Feature-Score — **keine Wahrscheinlichkeit, kein erwarteter Return und kein kalibrierter Edge**. Kosten- und Risk-Gates entscheiden unabhängig weiter; Edge-Kalibrierung bleibt gesperrt, bis eine neue, ausreichend große und zeitlich saubere Out-of-Sample-/Forward-Evaluation besteht.

## Vorherige Regel (`tsm-0.1.2`)

Die vorherige Version nutzte dieselben Momentum- und Mittelwertbedingungen, jedoch keinen Trendeffizienzfilter. Ihre Beobachtungen und Ergebnisse bleiben für Vergleichszwecke erhalten, dürfen nicht als Ergebnisse der neuen Strategieversion gezählt werden.

## Wissenschaftliche Motivation (Hypothese, kein Profitabilitätsnachweis)

- Deprez & Frömmel (2024), *Are simple technical trading rules profitable in bitcoin markets?*, International Review of Financial Analysis: https://doi.org/10.1016/j.iref.2024.05.003
- *Momentum in the Cryptocurrency Market: A Comprehensive Analysis under Realistic Assumptions* (2026), Review of Asset Pricing Studies: https://doi.org/10.1093/rapstu/raag011
- Bailey et al., *The Probability of Backtest Overfitting*: https://escholarship.org/uc/item/4w1110bb
- Hurst, Ooi & Pedersen, *A Century of Evidence on Trend-Following Investing*: https://www.aqr.com/Insights/Research/Journal-Article/A-Century-of-Evidence-on-Trend-Following-Investing

### Aktueller Evidenzstand (geprüft am 16. Oktober 2026)

Han, Kang & Ryu, *Momentum in the Cryptocurrency Market: A Comprehensive Analysis under Realistic Assumptions*, Review of Asset Pricing Studies, online veröffentlicht am 13. Oktober 2026, DOI 10.1093/rapstu/raag011. Die veröffentlichte Zusammenfassung berichtet starke Evidenz für Time-Series-Momentum, nahezu keine für Cross-Sectional-Momentum und zeigt, dass unter täglicher Mark-to-Market-Bewertung viele Cross-Sectional-Portfolios liquidiert werden; statistisch signifikante Mittelwertrenditen führten dort teilweise zu negativen Gewinnen. Die Autoren betonen fette Verteilungsschwänze und dass der Mittelwert für Profitabilitätstests nicht genügt. Quelle: https://academic.oup.com/raps/advance-article-abstract/doi/10.1093/rapstu/raag011/8914819

### Ergänzende Intraday-Evidenz (Online geprüft am 24. Oktober 2026)

Zwei zusätzliche Arbeiten sind für unsere H1-Regel besonders relevant, weil sie zeigen, weshalb ein einziger Momentum-Schwellenwert nicht als universell gültig angenommen werden darf:

- *Intraday return predictability in the cryptocurrency markets: Momentum, reversal, or both* (2022): Die Studie untersucht hochfrequente Bitcoin-Daten von 2013 bis 2020 und berichtet sowohl Intraday-Momentum als auch Reversal. Die Ergebnisse hängen laut Abstract unter anderem von großen Kurssprüngen, Liquidität und bestimmten Markt-/Ereignisphasen ab; ähnliche Muster werden für ETH, LTC und XRP berichtet. Quelle: https://www.sciencedirect.com/science/article/pii/S1062940822000833
- *On the Performance of Lagged Momentum and Reversal Strategies Across Daytime and Overnight Sessions in Bitcoin and Ethereum Cryptocurrencies* (2026): Diese Studie verwendet stündliche Kraken-Kurse von 2016 bis 2025 und vergleicht geordnete Momentum-/Reversal-Regeln über verschiedene 12-Stunden-Grenzen. Die berichteten bevorzugten Regeln unterscheiden sich zwischen BTC und ETH; die Autoren ergänzen Transaktionskostenbetrachtung, Mehrfachtestprüfung und einen chronologischen Holdout. Quelle: https://www.mdpi.com/1911-8074/19/9/692
- Bailey und López de Prado beschreiben, warum das Durchprobieren vieler historischer Varianten eine scheinbar gute, tatsächlich aber überangepasste Regel auswählen kann. Das ist für unsere kleine, zeitlich überlappende Shadow-Stichprobe besonders relevant. Quelle: https://academic.oup.com/jrssig/article/18/6/22/7038278

Diese Arbeiten belegen **nicht**, dass eine H1-Reversal- oder Session-Strategie für unser Konto profitabel wäre. Sie rechtfertigen nur neue, vorab definierte Forschungsfragen: Long und Short getrennt auswerten; eine kleine Zahl von Session-/Regime-Baselines gegen die aktuelle Regel vergleichen; alle Varianten dokumentieren; und abschließende chronologische Testdaten nicht zur Optimierung verwenden. Bis diese Analysen und realistische Netto-Kostenrechnungen bestanden sind, wird die H1-Strategie nicht geändert und nicht freigegeben.

Konsequenzen für diese Implementierung:

- Die vorhandene 24-/72-Stunden-Regel bleibt eine zu prüfende Time-Series-Momentum-Hypothese; sie wird nicht als Renditeprognose mit bekannter Trefferwahrscheinlichkeit interpretiert.
- Ein positiver Momentum-Proxy gilt nicht als kalibrierter Netto-Edge. Das unabhängige Risikogate verlangt explizit `edge_calibrated=True`; der Runtime-Pfad übergibt `False`, bis OOS-/Forward-Belege aus dem unveränderten Modell und nach realistischen Kosten vorliegen.
- Bei gehebelten Positionen sind Liquidationspfad, Gap-/Tail-Loss, Margin-Level, Funding-/Rollover-Währung und unvollständige Order-/Ledger-Zuordnung Freigabekriterien, keine optionalen Komfortmetriken.
- Backtest-Ergebnisse benötigen Mehrfachtest-/Overfitting-Kontrollen und einen nicht zur Parametersuche verwendeten finalen Test; eine gute In-Sample-Mittelrendite genügt nicht.

Die Studien betrachten andere Stichproben, Regeln, Zeiträume und Ausführungsannahmen. Sie belegen nicht, dass dieses Modell für dieses Konto oder gehebelte Kraken-Spot-Margin-Trades profitabel ist. Krypto-Returns sind häufig fett-schwänzig und Drawdowns können steil sein; der Mittelwert allein reicht nicht zur Risikobeurteilung.

## Laufzeit-Shadow-Research ab v0.1.40

Die Research-Zusammenfassung filtert verbindlich nach der aktiven Strategieversion. Ergebnisse von `tsm-0.1.2` werden weder in Stichprobengröße noch Train-/Validation-/Test-Kennzahlen der neuen `tsm-0.2.0`-Regel gezählt. Ein Versionswechsel startet eine separate Evaluationsserie; frühere Beobachtungen bleiben persistent, aber getrennt.

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