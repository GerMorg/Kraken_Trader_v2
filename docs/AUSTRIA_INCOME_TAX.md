# Österreichische Einkommensteuer — Datenaufbereitung

## Rechtsquellen und Grenze

- BMF, steuerliche Behandlung von Kryptowährungen: https://www.bmf.gv.at/themen/steuern/sparen-veranlagen/steuerliche-behandlung-von-kryptowaehrungen.html
- BMF, Steuerreporting: https://www.bmf.gv.at/themen/steuern/sparen-veranlagen/steuerreporting.html
- EStG 1988: einschlägige Bestimmungen sind im jeweiligen Berichtsjahr in aktueller Fassung zu prüfen.

Der Export der App ist **keine Steuerberatung und kein amtliches BMF-Steuerreporting**. Insbesondere darf man die steuerliche Behandlung gehebelter Margin-Eröffnung, Schließung, Kreditrückzahlung, USDC-Konvertierung und Settlement nicht pauschal mit einem ungehebelten Spot-Trade gleichsetzen. Unklare Fälle werden für die steuerliche Prüfung markiert und nicht stillschweigend klassifiziert.

## Jahresdateien

Für jedes Jahr unter `/data/reports/tax/AT/<Jahr>/` erzeugt die Implementierung:

- `income-tax-report.json`
- `tax-events.csv`
- `income-tax-information.md`

Der Runtimezyklus aktualisiert den Jahresbericht aus den bereits in SQLite gespeicherten Steuerereignissen. Er nennt IDs mit fehlender EUR-Bewertung, unbestätigten Daten oder erforderlicher manueller Prüfung. Die Quellabdeckung wird ausdrücklich als unvollständig zurückgegeben, weil das App-Runtime aktuell keine vollständige Trade-/Ledger-/Transfer-Synchronisation aus Kraken durchführt. Es werden keine FX-Kurse, Anschaffungskosten, Kreditbeträge oder Gebühren erfunden.

## Ereignisse mitzuverfolgen

Spot-Fills; tatsächliche Gebühren inklusive Gebührenwährung; Positionseinstieg/-schließung; Margin-Loan/Repayment und Settlement; Rollover-/Fundingkosten; Konversions- und Ledgerbuchungen; EUR-Umrechnung mit Kursquelle/Zeitpunkt; Einzahlungen, Auszahlungen und manuelle Transfers; nicht zuordenbare externe Trades bzw. Restbestände.

## Noch zu vervollständigen

Der paginierte, idempotente Import von Kraken Trades History und Ledgers wurde begonnen und läuft seitenweise mit persistenten Cursorn. Die Trades-History-Importvalidierung verlangt nun das Exchange-Feld `margin`: ein nachweislich positiver Betrag wird als `MARGIN_TRADE_FILL`, null als `SPOT_TRADE_FILL` klassifiziert. Das tatsächliche Feld, der Betrag, der Produkttyp und soweit geliefert die verknüpften Ledger-IDs werden im Event-Payload bewahrt. Fehlende oder ungültige Werte halten den Backfill unvollständig, statt den Vorgang als Spot zu erraten. Diese technische Produktklassifikation ist keine rechtliche Steuerklassifikation. Noch nicht fertig sind der vollständige Import-Nachweis, Abstimmung zu Orders/Positionen/Transfers, zeitpunktbezogene FX-Auflösung, Bewertung von Gebühren in Fremdwährungen, Zuordnung der Finanzierung zur betroffenen Position, Alt-/Neuvermögen-Prüfung anhand einer lückenlosen Historie und die rechtliche Steuerklassifikation der Margin-Ereignisse. Der aktuelle Export ist somit keine vollständige Steuerbuchhaltung, sondern nur die Export-/Warninfrastruktur.