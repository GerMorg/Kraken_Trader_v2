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

Die aktuelle Implementation exportiert vorhandene steuerliche Events und nennt IDs mit fehlender EUR-Bewertung, unbestätigten Daten oder erforderlicher manueller Prüfung. Sie erfindet keine FX-Kurse, Anschaffungskosten, Kreditbeträge oder Gebühren.

## Ereignisse mitzuverfolgen

Spot-Fills; tatsächliche Gebühren inklusive Gebührenwährung; Positionseinstieg/-schließung; Margin-Loan/Repayment und Settlement; Rollover-/Fundingkosten; Konversions- und Ledgerbuchungen; EUR-Umrechnung mit Kursquelle/Zeitpunkt; Einzahlungen, Auszahlungen und manuelle Transfers; nicht zuordenbare externe Trades bzw. Restbestände.

## Noch zu vervollständigen

Der vollständige Import und die idempotente Wiederabfrage von Kraken Trades History und Ledgers, zeitpunktbezogene FX-Auflösung, Bewertung von Gebühren in Fremdwährungen, Zuordnung der Finanzierung zur betroffenen Position, Alt-/Neuvermögen-Prüfung anhand einer lückenlosen Historie und der Steuerprüfung unterliegende Klassifikation der Margin-Ereignisse sind nicht fertig. Der aktuelle Export ist somit keine vollständige Steuerbuchhaltung, sondern nur die Export-/Warninfrastruktur.