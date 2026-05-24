#!/usr/bin/env python3
import argparse
import csv
import re
from pathlib import Path
from typing import Dict, List, Optional

import pdfplumber


PATTERNS = {
    "arbejdstimer": [
        r"Norm\.timer[\s:]*([\d]{1,4}[,.]\d{1,2})",
        r"Betalte timer[\s:]*([\d]{1,4}[,.]\d{1,2})",
        r"Arbejdstid[\s:]*([\d]{1,4}[,.]\d{1,2})",
        r"Arbejdstimer[\s:]*([\d]{1,4}[,.]\d{1,2})",
        r"Timer[\s:]*([\d]{1,4}[,.]\d{1,2})",
    ],
    "sygdom_days": [
        r"Sygedage[\s:]*([\d]{1,4}[,.]\d{1,2})",
        r"Sygdom[\s:]*([\d]{1,4}[,.]\d{1,2})",
        r"Fravær[\s:]*([\d]{1,4}[,.]\d{1,2})",
    ],
    "ferie_days": [
        r"Feriedage[\s:]*([\d]{1,4}[,.]\d{1,2})",
        r"Ferie[\s:]*([\d]{1,4}[,.]\d{1,2})",
        r"Afholdt ferie[\s:]*([\d]{1,4}[,.]\d{1,2})",
    ],
    "brutto": [
        r"A-Indkomst[\s:]*([\d]{1,3}(?:[.\s]\d{3})*(?:[,.]\d{2})?)",
        r"AM-grundlag[\s:]*([\d]{1,3}(?:[.\s]\d{3})*(?:[,.]\d{2})?)",
        r"Ferieberettiget løn[\s:]*([\d]{1,3}(?:[.\s]\d{3})*(?:[,.]\d{2})?)",
        r"Bruttoløn[\s:]*([\d]{1,3}(?:[.\s]\d{3})*(?:[,.]\d{2})?)",
        r"Brutto[\s:]*([\d]{1,3}(?:[.\s]\d{3})*(?:[,.]\d{2})?)",
        r"Løn før skat[\s:]*([\d]{1,3}(?:[.\s]\d{3})*(?:[,.]\d{2})?)",
    ],
    "netto": [
        r"Til udbetaling[\s:]*([\d]{1,3}(?:[.\s]\d{3})*(?:[,.]\d{2})?)",
        r"Udbetalt[\s:]*([\d]{1,3}(?:[.\s]\d{3})*(?:[,.]\d{2})?)",
        r"Netto[\s:]*([\d]{1,3}(?:[.\s]\d{3})*(?:[,.]\d{2})?)",
    ],
    "pension_employee": [
        r"Medarbejderbidrag[\s:]*([\d]{1,3}(?:[.\s]\d{3})*(?:[,.]\d{2})?)",
        r"Arbejdstagerpension[\s:]*([\d]{1,3}(?:[.\s]\d{3})*(?:[,.]\d{2})?)",
        r"Pension egen[\s:]*([\d]{1,3}(?:[.\s]\d{3})*(?:[,.]\d{2})?)",
    ],
    "pension_employer": [
        r"Virksomhedsprocent.*?([\\d]{1,3}(?:[.\\s]\\d{3})*(?:[,.]\\d{2})?)",
        r"Arbejdsgiverpension[\s:]*([\d]{1,3}(?:[.\s]\d{3})*(?:[,.]\d{2})?)",
        r"Pension arbejdsgiver[\s:]*([\d]{1,3}(?:[.\s]\d{3})*(?:[,.]\d{2})?)",
        r"Pension overføres.*?([\d]{1,3}(?:[.\s]\d{3})*(?:[,.]\d{2})?)",
    ],
}


def norm_number(value: str) -> Optional[float]:
    if value is None:
        return None
    text = str(value).strip().replace("kr.", "").replace("kr", "")
    text = text.replace(" ", "")
    if not text:
        return None
    if text.count(",") == 1 and text.count(".") >= 1:
        text = text.replace(".", "").replace(",", ".")
    else:
        text = text.replace(",", ".")
    m = re.search(r"-?\d+(?:\.\d+)?", text)
    return float(m.group()) if m else None


def extract_first(patterns: List[str], text: str) -> Optional[float]:
    for pattern in patterns:
        m = re.search(pattern, text, flags=re.IGNORECASE | re.MULTILINE | re.DOTALL)
        if m:
            return norm_number(m.group(1))
    return None


def extract_ferie_days(text: str) -> Optional[float]:
    m = re.search(
        r"FERIEDAGE.*?I alt\s+([\d]{1,3}[,.]\d{1,2})\s+([\d]{1,3}[,.]\d{1,2})\s+([\d]{1,3}[,.]\d{1,2})",
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if m:
        return norm_number(m.group(2))
    return None


def read_pdf_text(pdf_path: Path) -> str:
    parts = []
    with pdfplumber.open(str(pdf_path)) as pdf:
        for page in pdf.pages:
            txt = page.extract_text() or ""
            if txt:
                parts.append(txt)
    return "\n".join(parts)


def parse_payslip_text(text: str) -> dict:
    return {
        "arbejdstimer": extract_first(PATTERNS["arbejdstimer"], text),
        "sygdom_days": extract_first(PATTERNS["sygdom_days"], text),
        "ferie_days": extract_ferie_days(text),
        "brutto": extract_first(PATTERNS["brutto"], text),
        "netto": extract_first(PATTERNS["netto"], text),
        "pension_employee": extract_first(PATTERNS["pension_employee"], text),
        "pension_employer": extract_first(PATTERNS["pension_employer"], text),
    }


def analyze_payslip(pdf_path: Path, debug: bool = False) -> Dict[str, object]:
    text = read_pdf_text(pdf_path)
    if debug:
        print(f"\n===== {pdf_path.name} =====")
        print(text[:20000])
        print("===== END =====\n")
    data = parse_payslip_text(text)
    data["file"] = pdf_path.name
    return data


def find_pdfs(folder: Path) -> List[Path]:
    return sorted([p for p in folder.rglob("*.pdf") if p.is_file()])


def write_csv(rows: List[Dict[str, object]], output_path: Path) -> None:
    if not rows:
        return
    fieldnames = list(rows[0].keys())
    with output_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def print_table(rows: List[Dict[str, object]]) -> None:
    if not rows:
        print("Ingen lønsedler fundet.")
        return
    headers = ["Fil", "Arbejdstimer", "Sygdom (dage)", "Ferie (dage)", "Brutto", "Netto"]
    print("\n" + " | ".join(headers))
    print("-" * 105)
    for r in rows:
        print(f"{r['file']} | {r['arbejdstimer']} | {r['sygdom_days']} | {r['ferie_days']} | {r['brutto']} | {r['netto']}")


def main():
    parser = argparse.ArgumentParser(description="Læs DataLøn/payslip PDF-filer og udtræk timer/løn til CSV")
    parser.add_argument("--mappe", required=True, help="Mappe med PDF-lønsedler")
    parser.add_argument("--csv", help="Gem resultat til CSV-fil")
    parser.add_argument("--debug", action="store_true", help="Vis rå tekst fra PDF for fejlsøgning")
    args = parser.parse_args()

    folder = Path(args.mappe)
    if not folder.exists() or not folder.is_dir():
        raise SystemExit(f"Mappe findes ikke: {folder}")

    pdfs = find_pdfs(folder)
    if not pdfs:
        raise SystemExit(f"Ingen PDF-filer fundet i: {folder}")

    rows = []
    for pdf in pdfs:
        try:
            row = analyze_payslip(pdf, debug=args.debug)
            rows.append(row)
        except Exception as e:
            rows.append({
                "file": pdf.name,
                "arbejdstimer": None,
                "sygdom_days": None,
                "ferie_days": None,
                "brutto": None,
                "netto": None,
                "pension_employee": None,
                "pension_employer": None,
            })
            print(f"Fejl i {pdf.name}: {e}")

    print_table(rows)

    if args.csv:
        out = Path(args.csv)
        out.parent.mkdir(parents=True, exist_ok=True)
        write_csv(rows, out)
        print(f"\nCSV gemt: {out}")


if __name__ == "__main__":
    main()