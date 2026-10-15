# Strategie- und Research-Hypothese

## Kandidat v1

Der erste Kandidat ist eine einfache H1-Time-Series-Momentum-/Trendfolge-Regel, kein Machine-Learning-Modell. Sie berechnet 24-Kerzen- und 72-Kerzen-Renditen in Basispunkten sowie 24-/72-Kerzen-Mittelwerte. LONG bzw. SHORT setzt ein gleichgerichtetes Momentum beider Zeitfenster und eine passende Mittelwertbestätigung voraus. Bei unzureichender Historie wird kein Signal erzeugt.

Die Standardschwellen sind 120 bps über 24 Stunden und 180 bps über 72 Stunden. Sie sind vorab festgelegte Forschungsparameter, nicht empirisch optimierte oder als profitabel nachgewiesene Werte. Das interne Signal-Score ist ausdrücklich **keine kalibrierte Wahrscheinlichkeit**. Die Kostenfilter sollen Spread, Ausführungsgebühr, Slippage und Margin-Gebühren vor einer Entscheidung einbeziehen.

## Wissenschaftliche Motivation (Hypothese, kein Profitabilitätsnachweis)

- Deprez & Frömmel (2024), *Are simple technical trading rules profitable in bitcoin markets?*, International Review of Financial Analysis: https://doi.org/10.1016/j.iref.2024.05.003
- *Momentum in the Cryptocurrency Market: A Comprehensive Analysis under Realistic Assumptions* (2026), Review of Asset Pricing Studies: https://doi.org/10.1093/rapstu/raag011
- Bailey et al., *The Probability of Backtest Overfitting*: https://escholarship.org/uc/item/4w1110bb
- Hurst, Ooi & Pedersen, *A Century of Evidence on Trend-Following Investing*: https://www.aqr.com/Insights/Research/Journal-Article/A-Century-of-Evidence-on-Trend-Following-Investing

Die Studien betrachten andere Stichproben, Regeln, Zeiträume und Ausführungsannahmen. Sie belegen nicht, dass dieses Modell für dieses Konto oder gehebelte Kraken-Spot-Margin-Trades profitabel ist. Krypto-Returns sind häufig fett-schwänzig und Drawdowns können steil sein; der Mittelwert allein reicht nicht zur Risikobeurteilung.

## Stand des Simulators

`app/backtest.py` bietet chronologische Train-/Validation-/Test-Splits und einen OHLCV-basierten Szenario-Simulator mit begrenzter Participation, Gebühren, Spread, Slippage, Haltedauer, Margin-Gebühren und konservativem Gap-Stop. Das ist **noch kein realistischer Börsen-Ausführungssimulator**: historische Orderbücher, tatsächliche Maker-/Taker-Fill-Wahrscheinlichkeit, Tiefe/Impact, historische instrumentbezogene Fees/Minima, echte Teilfüllungs-/Cancel-/Reject-Semantik, USDC-Kreditzuweisung und echte Margin-Ledgerdaten sind nicht enthalten. Im jetzigen Repository gibt es außerdem keinen qualitätsgesicherten historischen Datensatz, aus dem ein belastbarer OOS-Nachweis erzeugt wurde.

Daher kennzeichnet der Code seine Kennzahlen als `SCENARIO_ONLY_NOT_LIVE_VALIDATION`. Er soll weder Live-Orders freigeben noch automatische Strategiepromotion auslösen.

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