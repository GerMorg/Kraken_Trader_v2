# Version 0.1.76 — Validierung und Release-Grenzen

## Änderungen

- Aktive H1-Kandidaten: TSM-Baseline und Fourier Cycle; die unbewährten Mean-Reversion-, Logistik- und Volatility-Breakout-Kandidaten werden nicht mehr erzeugt.
- Additive SQLite-Tabelle für Futures-Demo-Order-Intents; bestehende Tabellen und historische Research-Daten bleiben erhalten.
- Eigenständige Futures-Signallogik und Futures-Risikogrenzen; keine Wiederverwendung des Spot-RiskSnapshot oder der Spot-Order-Authority.
- Futures-Demo-Adapter unterstützt Send/Edit/Cancel, jedoch nur über den festen Demo-Host und separate Demo-Credentials.
- Futures-Produktion bleibt Read-only; INTERNAL_LIVE_EXECUTION_RELEASED bleibt false.
- Portfolio-Risikostatus wird pro Kontozyklus unabhängig von einem Richtungssignal ausgegeben.

## Futures Demo Adapter — noch offene E2E-Prüfungen

- [ ] Echter Kraken-Demo-Key mit General API Full Access und Transfer No Access
- [ ] Market-/Limit-Entry, Exchange-Ablehnung und ausreichende/ungenügende Collateral-Fälle
- [ ] Teilfüllung, vollständiger Fill und Cancel-Rennen
- [ ] Timeout nach Submit mit anschließendem Status-/Fill-Reconciliation ohne erneuten Submit
- [ ] Reduce-only Stop für Long und Short, Mark-Trigger und tatsächliche Positionsreduktion
- [ ] Restart zwischen Submit, Ack, Fill und Schutzorder
- [ ] Gebühren-, Funding-, Conversion- und Collateral-Abgleich im EEA Multi-Collateral-Wallet
- [ ] Dynamische Allowlist aus tatsächlich handelbaren Futures-PF-Instrumenten und Spot-USD-Krypto-Basisassets; keine xStocks, Fixed-Maturity-, inverse, Index- oder Rohstoffprodukte
- [ ] Initial-Margin-Wert pro tatsächlicher Instrument-Size-Tier aus der Kraken-Instrumentantwort; fehlende Tierdaten blockieren den Einstieg (kein Notional/Leverage-Proxy)

Der Adapter wird in 0.1.76 nicht automatisch aus dem normalen Entscheidungszyklus aufgerufen. Die Unit-Tests beweisen ausschließlich die lokale Zustands-/Requestlogik, nicht den Austausch mit Kraken Demo.

## Fourier

- [ ] Historische OHLCV-Daten mehrerer unabhängiger Marktphasen auswerten
- [ ] Gleiche H1-Entscheidungszeitpunkte und dieselben Kostenannahmen wie TSM verwenden
- [ ] Long/Short, Spread, Slippage, Funding und nicht ausgeführte Orders untersuchen
- [ ] Unangetasteten Out-of-Sample-Test und spätere Forward-Beobachtung abwarten

Fourier ist nicht in die Produktions-Orderentscheidung eingebunden.

## Freigabe

Es gibt keinen neuen Produktions-Futures-Orderpfad. Futures-Produktions-API besitzt ausschließlich Read-only-Methoden. Demo-Orders können nur über die separat instanziierte FuturesDemoTradingClient-Komponente und separate Demo-Zugangsdaten gesendet werden.
