# Multi-Asset-Research: Rebalancing und Relative-Value-Pairs

## Zweck und Abgrenzung

Ab App-Version 0.1.53 untersucht der bestehende SHADOW-Zyklus drei weitere, strikt getrennte Research-Ansätze:

- **Variante A — Volatility Harvesting / Parrondo-inspiriertes Rebalancing:** Ein Portfolio aus aktuell stark negativ korrelierten USD-Krypto-Paaren wird dynamisch ausgewählt und bei Bedarf auf Zielgewichte zurückgesetzt.
- **Variante B — Relative-Value-/Mean-Reversion-Pairs:** Zwei positiv gemeinsam bewegte Assets werden anhand eines rollierenden logarithmischen Preis-Spreads untersucht. Bei einer großen Abweichung wird ein hypothetischer Long-Spread oder Short-Spread bewertet.
- **Variante C — Cross-Sectional Momentum Long/Short:** USD-Krypto-Assets werden anhand eines festen 24-/72-H1-Momentum-Scores gerankt. Die stärksten drei positiven Assets werden long, die schwächsten drei negativen Assets short gehalten.

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
- Ein Exit-Signal wird ebenfalls erst nach Kerzenschluss erkannt. Die hypothetische Ausführung erfolgt frühestens am Open der darauffolgenden H1-Kerze, nicht zum selben Schlusskurs.
- Z-Score mindestens +2,0: `SHORT_SPREAD` (Asset A short, Asset B long).
- Z-Score höchstens -2,0: `LONG_SPREAD` (Asset A long, Asset B short).
- Ausstieg bei Rückkehr auf |Z| <= 0,5.
- Adverser Spread-Stop bei |Z| >= 4,0.
- Maximal 48 Stunden Haltedauer.
- Die zum Einstieg geschätzte Hedge-Ratio wird für den gesamten Trade eingefroren.
- Trades überlappen sich pro Paar nicht. Trades, deren Ziel über eine Train-/Validation-/Test-Grenze läuft, werden als Boundary-Purged markiert.

Das hypothetische Pair-PnL wird auf ein kombiniertes Brutto-Notional von 1 normiert; die Leg-Gewichte berücksichtigen die Hedge-Ratio und das relative Kursniveau.

### Gemeinsame Pair-Portfolio-Simulation

Die Kandidaten aller Paare werden zusätzlich zeitlich gemeinsam simuliert:

- maximal vier gleichzeitig offene Pairs;
- 25 % des anfänglichen Equity als kombiniertes Brutto-Notional je Pair, maximal 100 % Gesamt-Brutto-Notional;
- ein Asset darf nicht gleichzeitig in mehreren offenen Pairs vorkommen;
- Konflikte werden deterministisch nach Entry-Zeitpunkt, danach absteigendem Betrag des Entry-Z-Scores und Symbolnamen priorisiert;
- Pair-Positionen werden stündlich mark-to-market bewertet; Ein- und Ausstiegskosten werden am jeweiligen Open angesetzt;
- die Slippage-Sensitivität wird für 0/1/2/5/10 bp je Seite ausgewiesen.

Die Allokation bleibt auf das anfängliche Equity bezogen und wird nicht automatisch reinvestiert. Es wird kein Hebel eingesetzt und keine Kraken-Margin-/Liquidationslogik simuliert. `portfolio_pnl: false` bleibt korrekt, da es sich nicht um echte Fill- oder KontopnL handelt.

### Kraken-Margin-Prüfung

Aktuelle Signale nennen für jedes Leg die benötigte Kraken-Richtung (BUY oder SELL) und prüfen diese gegen die von Kraken gelieferten Instrument-Metadaten (`leverage_buy` / `leverage_sell`). Das bestätigt weder Kontomargin, Kreditverfügbarkeit, tatsächliche API-Handelsrechte, Mindestmengen, verfügbare Margin noch eine ausführbare Order. Die Margin-Eröffnungs- und Rollover-Kosten sind in der Nettoauswertung ausdrücklich **nicht enthalten**. Deshalb darf ein statistisch positives Spot-Proxy-Ergebnis nicht als Margin-Nettorendite interpretiert werden.

## Kostenmodell und Kennzeichnung

Bekannte aktuelle Kraken-Taker-Gebühren (wenn verfügbar), aktueller Ticker-Spread sowie 5 Basispunkte Slippage je Seite bleiben die Baseline-Historien-Proxys. Zusätzlich werden 0/1/2/5/10 bp Slippage-Szenarien und Break-even-Slippage ausgewiesen. Die 0-bp-Variante ist eine mathematische Untergrenze, kein garantiert ausführbarer Preis. Bei fehlenden Gebühren- oder Spreadinformationen wird keine Netto-Kennzahl berechnet; fehlende Kosten werden nicht auf null gesetzt.

Für Variante B werden beide Legs beim Ein- und Ausstieg belastet. Zusätzlich werden zu jedem frischen gemeinsamen H1-Bar öffentliche Kraken-Depth-Snapshots (bis zu zehn sichtbare Levels) je Asset erfasst und Round-Trip-Kosten für 250/500/1.000/2.500 USD simuliert. Je sichtbarer Preisstufe werden höchstens 25 % der angezeigten Menge genutzt. Die L2-Auswertung nutzt denselben Snapshot für Kauf und Verkauf und ist daher nur ein aktueller Kostenindikator, kein historischer oder zeitlich ausführbarer Round-Trip.

Finanzierung, Borrow-/Margin-Open-Gebühren, Rollover, Liquidationskosten und kontospezifische Margin-Zinsen sind noch nicht modelliert. Alle Netto-Kennzahlen tragen deshalb den Status eines Kosten-Proxys.

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


## Variante C — Cross-Sectional Momentum Long/Short (0.1.53)

Die dritte Hypothese ist unabhängig von der TSM-Einzelmarkt-Trendregel und den Pair-Spread-Ansätzen. Sie ordnet das synchronisierte USD-Universum nach einer vorab festgelegten Kombination aus 24- und 72-Stunden-Rendite:

- Score = 40 % der 24-H1-Rendite + 60 % der 72-H1-Rendite.
- Die drei stärksten Assets mit positivem Score bilden das Long-Portfolio; die drei schwächsten mit negativem Score das Short-Portfolio.
- 40 % des Anfangskapitals je Seite; die Gesamt-Bruttoexponierung beträgt 80 %, die Nettoexponierung 0 %, 20 % bleiben Cash.
- Ein einzelnes Asset erhält höchstens 13,34 % Anfangskapital an Bruttoexponierung.
- Neuauswahl alle sechs Stunden. Scores verwenden nur geschlossene Balken bis zum Entscheidungszeitpunkt; hypothetische Ausführung erfolgt am nächsten H1-Open. Die PnL-Messung läuft vom Ausführungs-Open bis zum folgenden Open.
- Werden nicht mindestens drei positive und drei negative Kandidaten gefunden, bleibt das Portfolio vollständig in USD-Cash.
- Die Baseline verwendet Taker-Gebühren, aktuellen Spread-Proxy und 5 bp Slippage je Seite; die Sensitivität 0/1/2/5/10 bp bleibt separat.
- Ein alternatives Maker-Gebührenszenario ist nur eine Gebührenreferenz. Es simuliert weder Queue-Position noch Fill-Wahrscheinlichkeit und ist keine Primärkennzahl.
- Short-Legs benötigen Margin. Finanzierung, Borrow, Rollover, Liquidation und Kontomargin sind nicht enthalten. Das Modell nutzt keinen Hebel und erzeugt keine Orders.

Die Schwellen, Gewichte und Regeln werden nicht automatisch aus dem Testsegment optimiert. Ein positiver Backtest allein ist keine Promotion oder Live-Freigabe.

## Gebühren- und Orderbuch-Evidenz

Ab 0.1.53 fragt die App `TradeVolume` mit `fee_schedule=true` ab und protokolliert die aktuell für das Konto/Paar zurückgegebenen Taker- und, sofern separat vorhanden, Maker-Gebühren. Die Taker-Gebühr bleibt die Baseline für marketable/sofort ausführbare Research-Szenarien. Die Maker-Gebühr wird getrennt als theoretischer Gebührenvergleich ausgewiesen; eine Limit-Order kann nicht als Maker-Fill angenommen werden.

Öffentliche Kraken-Depth-Snapshots werden zusätzlich append-only in der bestehenden SQLite-Datenbank gespeichert (Tabelle `market_depth_snapshots`). Jede Aufnahme enthält Paar, Snapshotzeit, zugehörigen gemeinsamen H1-Balken, bis zu zehn Bid-/Ask-Levels und bekannte Gebühren-/Spreadfelder. Frühere Snapshots werden nicht überschrieben. Mit zeitlich getrennten Snapshots können spätere Entry-/Exit-Kostenanalysen entstehen. Die Migration ist additiv: Vorhandene Research-, Handels-, Order-, Fill- und Ledger-Daten bleiben erhalten.

Ein einzelnes öffentliches L2-Buch bleibt nur eine Momentaufnahme sichtbarer Liquidität. Es belegt weder Queue-Position, versteckte Liquidität, Fill-Wahrscheinlichkeit, Markteinfluss bei späterer Ausführung noch einen echten Fill. Exakte realisierte Kosten können erst aus tatsächlichen Order-/Fill-Daten derselben Ausführung gemessen werden. Fehlende Werte werden daher nicht zu null oder zu Maker-Gebühren umgedeutet.
