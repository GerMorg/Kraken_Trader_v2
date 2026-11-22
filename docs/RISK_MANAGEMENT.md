## Hebelauswahl im Regelbetrieb — 0.1.75

Die App wählt jetzt den höchsten vom jeweiligen Instrument und der Handelsrichtung unterstützten Hebel, jedoch nie mehr als den code-eigenen Hard-Cap von 3x. Wenn nur 2x verfügbar ist, wird 2x verwendet; nicht unterstützte Hebelstufen werden nicht geraten oder erzwungen.

Das ist eine Kapital-/Collateral-Effizienzentscheidung, kein Gebührenrabatt: Spot-Taker-Gebühren werden auf das gehandelte Nominalvolumen berechnet. Die Positionsgröße wird weiterhin aus dem Risikobudget, der konkreten Signal-Stopdistanz samt Gap-Puffer, den Kosten, dem maximalen Positionsanteil und den verfügbaren Sicherheiten abgeleitet. Der Hebel allein darf das Risikobudget nicht erhöhen. Portfolio-Brutto-/Nettoexposure, Margin-Auslastung, Tagesverlust und Drawdown bleiben separate harte Schranken.

