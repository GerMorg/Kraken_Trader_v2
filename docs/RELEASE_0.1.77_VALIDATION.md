# Version 0.1.77 — Validierung und Release-Grenzen

## Änderungen

- Fügt eine optionale Futures-Demo-Read-Synchronisierung in den normalen HA-Zyklus ein.
- Verwendet dafür ausschließlich die separaten HA-Felder \`kraken_futures_demo_api_key\` und \`kraken_futures_demo_api_secret\`; die Produktions-Futures-Zugangsdaten bleiben getrennt.
- Liest Accounts, offene Positionen, offene Orders, Fills, Leverage-Präferenzen, Instrumente und Ticker über den fest gepinnten Demo-Host.
- Demo-Snapshots werden mit \`environment=demo_read_only\` separat persistiert. Der bestehende Getter für Produktions-Read-only-Snapshots filtert Demo-Daten aus.
- Die Demo-Key-Berechtigungsstufen werden nicht verifiziert. Sie sind bewusst als \`UNVERIFIED\` gespeichert; erfolgreiche authentifizierte GET-Abfragen beweisen keine Write-Berechtigung.
- \`CIRCUIT_BREAKER_STATUS.breaker_blocks_new_entries\` beschreibt ausschließlich aktive persistierte Circuit Breaker. Der Portfolio-Risikoblock wird unabhängig über \`PORTFOLIO_ENTRY_RISK_STATUS\` ausgegeben.

## Gelesene Demo-Endpunkte

Die Runtime ruft die sieben GET-Endpunkte Accounts, Open Positions, Open Orders, Fills, Leverage Preferences, Instruments und Tickers sequenziell auf. Schlägt die authentifizierte Kontoabfrage fehl, werden die weiteren privaten Leseabfragen übersprungen. Ein Teilresultat wird als PARTIAL gespeichert; fehlende Daten werden nicht als leere Positionen oder Orders interpretiert.

## Sicherheitsgrenzen

- \`futures_demo_enabled\` ist standardmäßig \`false\`.
- Demo-Zugangsdaten sind getrennt von Produktions-Read-only-Schlüsseln.
- Dieser Runtime-Pfad sendet ausschließlich GET-Anfragen. Keine Send/Edit/Cancel-Anfrage, kein Transfer und keine Änderung von Leverage-Präferenzen.
- Der Produktions-Futures-Client bleibt \`production_read_only\`; es wird kein Produktions-Futures-Mutationspfad hinzugefügt.
- Spot-/Spot-Margin-Aufträge und deren Risikobaselines werden nicht verändert.
- Futures-Demo-Orderadapter und Order-State-Machine sind weiterhin nicht mit der normalen Entscheidungs-Runtime verbunden; in dieser Version werden keine Futures-Demo-Aufträge automatisch erzeugt.

## Konfiguration und manueller E2E-Test

1. Einen separaten Kraken-Futures-Demo-API-Key anlegen und ausschließlich die Demo-Zugangsdaten in die neuen HA-Felder eintragen.
2. \`futures_demo_enabled\` auf \`true\` setzen. Der bestehende Produktions-Read-only-Schalter und seine Zugangsdaten bleiben unverändert.
3. \`real_orders_enabled\` unverändert auf \`false\` belassen.
4. App-Log auf \`FUTURES_DEMO_DATA_SYNC\` prüfen: \`environment=DEMO_READ_ONLY\`, sieben GET-Endpunkte, \`orders_submitted=false\`, keine Zugangsdaten im Log.
5. Mit deaktiviertem Schalter prüfen, dass \`futures_demo_status=DISABLED\` bleibt und keine Demo-API-Verbindung aufgebaut wird.

## Noch nicht nachgewiesen

- Echter Kraken-Demo-End-to-End-Zugriff mit einem vom Nutzer konfigurierten Demo-Key (erfordert die HA-Konfiguration).
- Futures-Demo-Signal-, Kosten-, Funding- und Risikopipeline.
- Futures-Demo-Send/Edit/Cancel, Fills, Restart-Recovery und Schutzstop-Reconciliation über die Runtime.
- Out-of-sample-Validierung einer Futures-Strategie.

Die Unit-Tests validieren die Runtime-Steuerung, Trennung der Snapshots und den GET-only-Vertrag mit simulierten API-Antworten; sie ersetzen keinen Test gegen den echten Kraken-Demo-Service.
