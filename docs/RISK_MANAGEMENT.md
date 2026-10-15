# Risikomanagement — AGGRESSIVE_BUT_CAPPED

Das Profil erlaubt ausdrücklich Margin und Short-Handel als Designziel. Hebel erzeugt jedoch keinen eigenen Prognosevorteil; er skaliert Verluste, Kosten und Liquidationsrisiken. Die Limits hier sind **harte Anwendungshöchstgrenzen**, keine Garantie, dass das gewählte Risiko gering oder ein Trade profitabel sei.

## Source-owned Caps

| Grenze | Maximum / Mindestschutz |
|---|---:|
| Risikobudget pro Trade | 0,5 % Equity |
| Nominalwert je Position | 20 % Equity |
| Bruttoexposure | 80 % Equity |
| Nettoexposure | 50 % Equity |
| belegte Margin | 25 % Equity |
| Hebel | 3× |
| gleichzeitig offene Positionen | 4 |
| Tagesverlust | 3 % |
| Drawdown-Grenze | 8 % |
| freie Sicherheitenreserve | mindestens 20 % |
| app-eigener Margin-Level Entry-Floor | mindestens 300 %, sonst unbekannt = blockiert |

Konfigurationswerte dürfen diese Caps nur reduzieren, nicht vergrößern. Der 300-%-Entry-Floor ist eine interne Sicherheitsregel und **keine Aussage über Kr akens Liquidationsschwelle**. Werte für Equity, Tagesverlust und Drawdown müssen aus einem lückenlos rekonstruierten Kontostand samt externen Transfers stammen; die Basis dafür ist derzeit nicht vollständig implementiert, daher bleiben Entries gesperrt.

## Positionsgröße

Die Implementierung schätzt das Risiko aus Stopabstand, Kurslückenpuffer und Kostenpuffer:

- `risk_budget = equity × risk_per_trade`
- `notional = min(risk_budget / (stop + gap + cost), equity × position_cap)`

Bei gehebelten Positionen wird zusätzlich der Notionalwert auf verfügbare Sicherheiten mal zulässigen Hebel begrenzt. Fehlende Margin-Kosten, nicht-positive Equity, unvollständige Daten oder fehlende Reconciliation müssen blockieren. Mindestordergrößen sind niemals ein Grund, eine Position künstlich zu vergrößern.

## Circuit Breaker

Nicht erreichbarer Kraken-Status, API-Authentifizierungs-/Datenfehler und nicht abgeglichene Portfoliozustände müssen protokolliert und persistent gesperrt werden. In diesem ersten Kern werden aktive Circuit Breaker konservativ nicht automatisch aufgehoben; kontrollierte, protokollierte Wiederherstellung fehlt noch und ist eine Freigabebedingung.

## Validierung vor jeder späteren Freigabe

Testen: adverse Gap- und Spread-Szenarien, Gebührenschocks, USDC-Depeg, Margin-Rate-/Rollover-Anstieg, korrelierte Longs/Shorts, Teilfüllungen, offene/unklare Orders und Abweichung zwischen Wallet, Lots und Exchange-Positionen. Eine steigende Signalqualität darf keine unveränderlichen Caps umgehen.