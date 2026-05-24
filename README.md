# Løncheck

Et Python-værktøj til at sammenligne registrerede timer fra Minuba med data fra lønsedler, så du kan tjekke om din løn passer.

## Repo

Projektet ligger her:
[https://github.com/BenkaDk/Loen-Check](https://github.com/BenkaDk/Loen-Check)

## Formål

Projektet bruges til at:

- hente arbejdstimer, ferie og sygdom fra Minuba
- læse lønsedler fra en mappe
- sammenligne timer og løn måned for måned
- finde afvigelser mellem registreret tid og udbetalt løn
- lave en rapport, der kan bruges til løncheck, fx sammen med Dansk El-Forbund

## Funktioner

- Scraper timer fra Minuba
- Læser lønsedler automatisk
- Matcher data pr. måned
- Beregner forventet løn ud fra timeløn
- Markerer fejl og afvigelser
- Eksporterer resultater til CSV og JSON

## Privat Minuba-script

Minuba-delen er lavet i et privat repo og er derfor ikke offentliggjort her.  
Dette repo indeholder løncheck-delen og sammenligningen mellem Minuba-data og lønsedler.

## Projektstruktur

```text
loncheck/
├── minuba_timer.py
├── lonseddel_analyse.py
├── reconcile.py
├── requirements.txt
├── .gitignore
├── LICENSE
├── data/
│   ├── minuba_2025.csv
│   └── payslips_2025.csv
└── reports/
    ├── reconciliation_report.csv
    └── reconciliation_report.json
```

## Installation

### 1. Klon repoet

```bash
git clone https://github.com/BenkaDk/Loen-Check.git
cd Loen-Check
```

### 2. Opret virtuelt miljø

#### Linux / Arch

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

#### Windows

```powershell
python -m venv .venv
.\\.venv\\Scripts\\Activate.ps1
pip install -r requirements.txt
```

## Brug

### 1. Hent data fra Minuba

```bash
python minuba_timer.py --periode 2025 --csv data/minuba_2025.csv
```

### 2. Læs lønsedler

```bash
python lonseddel_analyse.py --mappe ./lønsedler --csv data/payslips_2025.csv
```

### 3. Sammenlign data

```bash
python reconcile.py \
  --minuba-csv data/minuba_2025.csv \
  --payslip-csv data/payslips_2025.csv \
  --hourly-rate 220 \
  --out-csv reports/reconciliation_report.csv \
  --out-json reports/reconciliation_report.json
```

## Hvad scriptet sammenligner

- arbejdstimer
- ferie
- sygdom
- betalte timer på lønsedlen
- brutto- og nettoløn
- pension, hvis det findes i data
- forventet løn ud fra timeløn

## Output

Scriptet laver:

- en CSV-rapport
- en JSON-rapport
- advarsler ved afvigelser

## Eksempel på input

### Minuba CSV

```csv
date,hours,category,note
2025-01-02,7.5,work,Normal arbejdsdag
2025-01-03,7.5,sick,Syg
2025-01-04,7.5,vacation,Ferie
```

### Lønseddel CSV

```csv
period,paid_hours,gross_salary,net_salary,pension_employee,pension_employer,vacation_hours_paid,sick_hours_paid
2025-01,160,35200,24500,1050,2100,7.4,7.4
```

## Noter

- Projektet er lavet til personlig lønkontrol.
- Det er især nyttigt, hvis arbejdsgiveren ikke giver direkte timeseddel-eksport i Minuba.
- Filer med private data bør ikke pushes til GitHub.

## Roadmap

- [ ] PDF-rapport
- [ ] GUI
- [ ] bedre matching af lønseddel-felter
- [ ] kontrakt-upload til timeløn og pension
- [ ] PyInstaller build til .exe

## License

This project is licensed under the MIT License.