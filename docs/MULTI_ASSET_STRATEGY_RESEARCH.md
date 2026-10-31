# Multi-Asset-Research: Rebalancing und Relative-Value-Pairs

## Zweck und Abgrenzung

Ab App-Version 0.1.51 untersucht der bestehende SHADOW-Zyklus zwei weitere, strikt getrennte Research-Ansätze:

- **Variante A — Volatility Harvesting / Parrondo-inspiriertes Rebalancing:** Ein Portfolio aus aktuell stark negativ korrelierten USD-Krypto-Paaren wird dynamisch ausgewählt und bei Bedarf auf Zielgewichte zurückgesetzt.
- **Variante B — Relative-Value-/Mean-Reversion-Pairs:** Zwei positiv gemeinsam bewegte Assets werden anhand eines rollierenden logarithmischen Preis-Spreads untersucht. Bei einer großen Abweichung wird ein hypothetischer Long-Spread oder Short-Spread bewertet.

Das ist eine Research-Erweiterung, keine automatische Strategieauswahl und keine Freigabe für autonomen Echtgeldhandel. Beide Ansätze sind vom produktiven TSM-Einstiegspfad und von der Order Authority getrennt. Es werden aus diesem Modul keine Order-Intents, Orders oder Fills erzeugt.

## Gemeinsame Datengrundlage

Der Runtime-Zyklus übergibt ausschließlich bereits abgerufene, geschlossene H1-OHLC-Kerzen aus den im aktuellen Zyklus beobachteten USD-Kryptomärkten. Die Marktserien werden anhand identischer UTC-Stundenzeitstempel synchronisiert.

- Mindestumfang: 240 gemeinsame, lückenlose H1-Kerzen.
- Es werden ausschließlich gemeinsame Stundenbalken verwendet.
- Eine fehlende Kerze wird niemals vorwärtsgefüllt, interpoliert oder synthetisch erzeugt.
- Bei einer Datenlücke wird nur der letzte zusammenhängende gemeinsame Abschnitt verwendet. Ist dieser zu kurz, wird kein Ergebnis als validiert ausgegeben.
- Pro gemeinsamem letzten Kerzenzeitpunkt entsteht höchstens ein persistierter Snapshot. Wiederholungen des 5-Minuten-Zyklus erzeugen keine Duplikate.

Die Beobachtung ist auf das aktuelle, nach Liquidität begrenzte Runtime-Universum angewiesen. Sie ist deshalb keine vollständige Untersuchung aller auf Kraken gelisteten USD-Paare und kann Auswahl-/Survivorship-Effekte enthalten.

## Variante A — negatives Korrelations-Rebalancing

### Regel

1. Für jedes Asset-Paar wird die Pearson-Korrelation der letzten 168 stündlichen Schlusskursrenditen berechnet.
2. Als Kandidaten gelten Paare mit einer Korrelation von höchstens -0,25.
3. Ein deterministisches greedy matching nimmt die am stärksten negativ korrelierten, untereinander disjunkten Paare. Ein Coin wird in einem Auswahlzeitpunkt höchstens einem Paar zugeordnet.
4. Die Auswahl wird alle 24 Stunden aktualisiert.
5. Alle ausgewählten Assets erhalten dasselbe Zielgewicht. Nicht ausgewählte Assets bleiben in USD-Cash.
6. Ein Rebalance wird bei geänderter Asset-Mitgliedschaft oder mindestens fünf Prozentpunkten Gewichtsabweichung ausgelöst.

Die Strategie verwendet kein Fremdkapital und keine Short-Positionen. Sie ist eine Spot-Portfolio-Regel. Sie testet eine Rebalancing-/Volatility-Harvesting-Hypothese; sie behauptet nicht, dass das klassische Parrondo-Paradoxon automatisch einen positiven Erwartungswert erzeugt.

### Vergleichsstrategien

Jeder historische Lauf stellt drei Portfoliokurven gegenüber:

- negatives Korrelations-Rebalancing mit dynamischer Paarauswahl und Drift-Schwelle;
- periodisches Equal-Weight-Rebalancing im 24-Stunden-Rhythmus;
- Equal-Weight-Buy-and-Hold mit einmaliger Startallokation.

Brutto-Kurve, geschätzte Spot-Netto-Kurve, Drawdown, annualisierte Kennzahlen, Turnover, Anzahl Rebalances, geschätzte Handelskosten und durchschnittlicher Cash-Anteil werden getrennt ausgegeben.

## Variante B — Relative-Value-/Pairs-Trading

### Statistischer Kandidatenfilter

Für jedes synchronisierte Asset-Paar wird auf einem rollierenden 168-H1-Fenster eine OLS-Regression der logarithmierten Schlusskurse geschätzt:

`log(P_A) = alpha + beta * log(P_B) + residual`

Ein Kandidat wird nur weiter untersucht, wenn:

- Beta zwischen 0,2 und 5,0 liegt;
- die Korrelation der einfachen Stundenrenditen mindestens 0,30 beträgt;
- die AR(1)-Schätzung des Regressionsresiduums positiv und kleiner als eins ist;
- die daraus geschätzte Residual-Halbwertszeit zwischen 2 und 72 Stunden liegt.

**Wichtige Einschränkung:** Die AR(1)-Halbwertszeit ist ein Filter-/Mean-Reversion-Proxy und kein formaler Engle-Granger-/ADF-Cointegrationstest. Es gibt noch keine formale Cointegration-p-Wert-Prüfung und keine Korrektur für Mehrfachtests. Der Filter kann damit scheinbar stabile Paare auswählen, die außerhalb des Messfensters nicht stabil bleiben.

### Signal, Richtung und Exit-Regeln

- Entscheidung nach dem Schluss der aktuellen Kerze; Einstieg wird frühestens am Open der nächsten H1-Kerze bewertet.
- Z-Score mindestens +2,0: `SHORT_SPREAD` (Asset A short, Asset B long).
- Z-Score höchstens -2,0: `LONG_SPREAD` (Asset A long, Asset B short).
- Ausstieg bei Rückkehr auf |Z| <= 0,5.
- Adverser Spread-Stop bei |Z| >= 4,0.
- Maximal 48 Stunden Haltedauer.
- Die zum Einstieg geschätzte Hedge-Ratio wird für den gesamten Trade eingefroren.
- Trades überlappen sich pro Paar nicht. Trades, deren Ziel über eine Train-/Validation-/Test-Grenze läuft, werden als Boundary-Purged markiert.

Das hypothetische Pair-PnL wird auf ein kombiniertes Brutto-Notional von 1 normiert; die Leg-Gewichte berücksichtigen die Hedge-Ratio und das relative Kursniveau. Die Rückrechnung ist ein Zwei-Leg-Signal-/Return-Simulator, noch keine gemeinsame Simulation aller gleichzeitig offenen Paare.

### Kraken-Margin-Prüfung

Aktuelle Signale nennen für jedes Leg die benötigte Kraken-Richtung (BUY oder SELL) und prüfen diese gegen die von Kraken gelieferten Instrument-Metadaten (`leverage_buy` / `leverage_sell`). Das bestätigt weder Kontomargin, Kreditverfügbarkeit, tatsächliche API-Handelsrechte, Mindestmengen, verfügbare Margin noch eine ausführbare Order. Die Margin-Eröffnungs- und Rollover-Kosten sind in der Nettoauswertung ausdrücklich **nicht enthalten**. Deshalb darf ein statistisch positives Spot-Proxy-Ergebnis nicht als Margin-Nettorendite interpretiert werden.

## Kostenmodell und Kennzeichnung

Bekannte aktuelle Kraken-Taker-Gebühren (wenn verfügbar), aktueller Ticker-Spread sowie fest angenommene 5 Basispunkte Slippage je Seite werden als historische Kosten-Proxys verwendet. Der aktuelle Spread ist kein historischer Spreadnachweis. Bei fehlender Gebühren- oder Spreadinformation wird keine Netto-Kennzahl berechnet; fehlende Kosten werden nicht auf null gesetzt.

Für Variante B werden die beiden Legs beim Ein- und Ausstieg kostenbelastet. Finanzierung, Borrow-/Margin-Open-Gebühren, Rollover, Liquidationskosten und kontospezifische Margin-Zinsen sind noch nicht modelliert. Alle Netto-Kennzahlen tragen deshalb den Status eines Kosten-Proxys.

## Chronologische Auswertung

Die Historie wird unverändert in Train (60 %), Validation (20 %) und Test (20 %) geteilt. Entscheidungen an einem Zeitpunkt verwenden ausschließlich Daten bis zu dieser geschlossenen Kerze. Bei Paar-Trades wird ein Abschnitt erst zugeordnet, wenn Einstieg und Ausstieg innerhalb desselben Zeitabschnitts liegen; grenzüberschreitende Trades werden ausgewiesen und nicht zur Kennzahl eines Abschnitts gezählt.

Das Testsegment darf nicht zur Schwellenwertauswahl oder Modellanpassung verwendet werden. Die Kennzahlen sind dennoch keine unabhängigen OOS-Live-Trades: Die rollierenden Fenster überlappen, viele Paar-Kombinationen werden getestet und Mehrfachtest-/Survivorship-Risiko ist noch nicht vollständig korrigiert.

## Persistenz und Ereignisse

Die idempotente Tabelle `multi_asset_research_runs` speichert je `research_key` einen unveränderlichen Research-Snapshot inklusive Methoden-, Kosten-, Daten- und Ergebnis-Metadaten. Ein zusätzlicher Index erlaubt den Abruf der neuesten Snapshots. Die Migration ist additiv; vorhandene Bars, Strategy-Candidate-Beobachtungen, Orders, Fills, Ledgers und bisherige Research-Datensätze werden weder gelöscht noch neu aufgebaut.

Das Ereignis `MULTI_ASSET_STRATEGY_RESEARCH_SUMMARY` wird nur beim erstmaligen Speichern eines neuen, frischen Snapshot ausgegeben. `CYCLE_SUMMARY` meldet zusätzlich Status, Symboluniversum, Variant-Status und Anzahl gespeicherter Snapshots.

## Entscheidungskriterien vor einer späteren Strategieauswahl

Eine Strategie wird nicht allein wegen einer positiven Durchschnittsrendite ausgewählt. Vor einer späteren, getrennten Freigabe müssen mindestens folgende Fragen positiv beantwortet sein:

1. Bleibt die Netto-Out-of-Sample-Rendite in mehreren chronologischen Marktphasen positiv?
2. Ist sie gegenüber Buy-and-Hold, periodischem Rebalancing und den bestehenden Strategie-Kandidaten überlegen, nachdem alle Kosten einheitlich und realistisch angesetzt sind?
3. Bleibt der Vorteil bei höheren Gebühren, Spreads und Slippage bestehen?
4. Sind Stichprobe, nicht überlappende Trades, Gewinn-/Verlustverteilung, maximaler Drawdown und Tail-Risiken ausreichend?
5. Bleibt die Paar-/Korrelationsbeziehung in einem prospektiven Forward-Shadow-Test erhalten?
6. Sind tatsächliche Kraken-Margin-Gebühren, verfügbare Leverage-Richtungen, Kredit-/Margin-Limits, Ausführung, Liquidation und Gesamtportfolio-Reconciliation ausreichend geprüft?
7. Ist das kombinierte Portfolio überlappender Strategien mit Positionsgrößen, Korrelationen, Gesamt-Exposure und Stressphasen gemeinsam simuliert?

Die App promotet keine Parameter und wählt keine Strategie automatisch. Eine spätere Auswahl und Freigabe ist ein separater Entwicklungs- und Prüfentscheid. `INTERNAL_LIVE_EXECUTION_RELEASED` bleibt unverändert `False`.
