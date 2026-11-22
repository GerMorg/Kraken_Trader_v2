# Fourier Cycle Candidate — Research-Spezifikation v0.1.0

## Ziel

Der Fourier-Kandidat prüft, ob die H1-Logrenditen eines Marktes eine dominante, zeitlich stabile zyklische Komponente besitzen, deren Phasenprojektion einen inkrementellen Informationswert gegenüber der TSM-Baseline liefert. Er ist ein Research-Kandidat, keine freigegebene Strategie.

## Methode

1. Verwende ausschließlich abgeschlossene, lückenlose H1-Kerzen.
2. Berechne logarithmische Ein-Stunden-Renditen; keine Preislevel direkt periodogrammieren.
3. Verwende zwei unmittelbar aufeinanderfolgende Fenster zu je 128 Renditen.
4. Subtrahiere das Fenstermittel und wende ein Hann-Fenster für die Spektralschätzung an.
5. Suche feste Frequenzbins mit Perioden von 6 bis 32 Stunden.
6. Berichte dominante Periode, Anteil der Peak-Power, normalisierte spektrale Entropie und Peak-zu-zweitem-Peak-Verhältnis.
7. Schätze die Phase der dominanten Komponente per Sinus-/Kosinus-Least-Squares auf dem Renditefenster und projiziere genau einen Schritt voraus.
8. Ein Kandidat darf nur eine Richtung melden, wenn die dominante Periode zwischen beiden Fenstern um höchstens 20 % abweicht, die Spektralkonzentration mindestens 0,25 beträgt, die normalisierte Entropie höchstens 0,82 beträgt, das Peak-Verhältnis mindestens 1,15 und die projizierte Amplitude mindestens 0,12 der Rendite-Standardabweichung erreicht.

Es werden keine Frequenzbänder, Schwellen oder Perioden anhand der Testdaten optimiert. Alle Schwellen sind feste, zu falsifizierende Startwerte.

## Interpretation und Grenzen

- Der Score ist Spektralkonzentration, keine kalibrierte Wahrscheinlichkeit.
- Die Phasenprojektion ist keine garantierte Kursprognose und kein erwarteter Nettoreturn.
- Das Modell unterstellt lokale Quasi-Periodizität innerhalb der beiden Fenster. Krypto-Renditen sind nicht stationär; Perioden können brechen oder wandern.
- Frequenzauflösung hängt von der endlichen Fensterlänge ab. Hann-Fenster reduzieren Leakage, beseitigen aber weder Rauschen noch Nichtstationarität.
- Die Fourier-Auswertung erfasst keine Orderbuch-Queue, Markttiefe, Ausführungslatenz, historische Funding-Raten oder Liquidation.
- Es findet keine automatische Parameterpromotion oder Ordererstellung statt.

## Akzeptanzkriterien für eine mögliche spätere Weiterentwicklung

Vor jeder Promotion muss der Kandidat gegenüber TSM auf denselben Instrumenten, Entscheidungszeitpunkten und Kapitalannahmen geprüft werden. Der Test umfasst mehrere unabhängige Marktregime, Long und Short getrennt, purged Train/Validation/Test-Splits, Gebühren und Spread, Slippage-Stress sowie Funding-Sensitivität für Futures. Der Testabschnitt bleibt unangetastet.

Ein positiver Mittelwert allein reicht nicht. Erforderlich sind positiver kostenbereinigter Out-of-Sample-Nettobeitrag, ausreichende Anzahl unabhängiger Trades, stabile Ergebnisse über mehrere Zeitabschnitte, kein unvertretbarer Drawdown und ein inkrementeller Nutzen gegenüber TSM. Bis dahin bleibt Fourier Research-only.
