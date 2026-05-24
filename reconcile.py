#!/usr/bin/env python3
import argparse
import csv
import json
import re
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from fpdf import FPDF
from fpdf.enums import XPos, YPos

MONTH_MAP = {
    "januar": "01",
    "februar": "02",
    "marts": "03",
    "april": "04",
    "maj": "05",
    "juni": "06",
    "juli": "07",
    "august": "08",
    "september": "09",
    "oktober": "10",
    "november": "11",
    "december": "12",
}


def normalize_decimal(value) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return None
    text = text.replace("kr.", "").replace("kr", "").replace(" ", "")
    if text.count(",") == 1 and text.count(".") >= 1:
        text = text.replace(".", "").replace(",", ".")
    else:
        text = text.replace(",", ".")
    m = re.search(r"-?\d+(?:\.\d+)?", text)
    return float(m.group()) if m else None


def file_to_period(filename: str) -> Optional[str]:
    stem = Path(filename).stem.lower().replace("_", " ").replace("-", " ").strip()
    for dk, mm in MONTH_MAP.items():
        if stem.startswith(dk):
            return f"2025-{mm}"
    m = re.search(r"(20\d{2})[-_/ ]?(\d{2})", stem)
    if m:
        return f"{m.group(1)}-{m.group(2)}"
    return None


def load_minuba_csv(path: Path) -> Dict[str, dict]:
    months = defaultdict(lambda: {"work_hours": 0.0, "sick_hours": 0.0, "vacation_hours": 0.0, "entries": []})
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            date_raw = row.get("date") or row.get("dato") or row.get("day") or row.get("Date")
            if not date_raw:
                continue
            try:
                y, m, _ = map(int, date_raw.split("-")[:3])
                period = f"{y:04d}-{m:02d}"
            except Exception:
                continue
            hours = normalize_decimal(row.get("hours") or row.get("timer") or row.get("antal_timer") or 0) or 0.0
            category = (row.get("category") or row.get("type") or row.get("kategori") or "").strip().lower()
            note = row.get("note") or row.get("tekst") or row.get("description") or ""
            if not category:
                low = note.lower()
                if any(k in low for k in ["syg", "sygdom", "fravær"]):
                    category = "sick"
                elif any(k in low for k in ["ferie", "vacation"]):
                    category = "vacation"
                else:
                    category = "work"
            if category in ("sick", "sygdom", "syg"):
                months[period]["sick_hours"] += hours
            elif category in ("vacation", "ferie"):
                months[period]["vacation_hours"] += hours
            else:
                months[period]["work_hours"] += hours
            months[period]["entries"].append({"date": date_raw, "hours": hours, "category": category, "note": note})
    return months


def load_payslips_csv(path: Path) -> Dict[str, dict]:
    months = defaultdict(lambda: {
        "paid_hours": None,
        "gross_salary": None,
        "net_salary": None,
        "pension_employee": None,
        "pension_employer": None,
        "vacation_days": None,
        "sick_days": None,
        "lines": []
    })
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            period = row.get("period") or row.get("måned") or row.get("month") or row.get("periode")
            if not period:
                period = file_to_period(row.get("file") or "")
            if not period:
                continue
            period = period[:7]
            rec = months[period]
            if rec["paid_hours"] is None:
                rec["paid_hours"] = normalize_decimal(row.get("arbejdstimer"))
            if rec["gross_salary"] is None:
                rec["gross_salary"] = normalize_decimal(row.get("brutto"))
            if rec["net_salary"] is None:
                rec["net_salary"] = normalize_decimal(row.get("netto"))
            if rec["vacation_days"] is None:
                rec["vacation_days"] = normalize_decimal(row.get("ferie_days"))
            if rec["sick_days"] is None:
                rec["sick_days"] = normalize_decimal(row.get("sygdom_days"))
            if rec["pension_employee"] is None:
                rec["pension_employee"] = normalize_decimal(row.get("pension_employee"))
            if rec["pension_employer"] is None:
                rec["pension_employer"] = normalize_decimal(row.get("pension_employer"))
            rec["lines"].append(row)
    return months


def reconcile(minuba: Dict[str, dict], payslips: Dict[str, dict], hourly_rate: Optional[float]):
    periods = sorted(set(minuba) | set(payslips))
    rows = []
    for period in periods:
        m = minuba.get(period, {})
        p = payslips.get(period, {})
        work = round(m.get("work_hours", 0.0), 2)
        sick_hours = round(m.get("sick_hours", 0.0), 2)
        vacation_hours = round(m.get("vacation_hours", 0.0), 2)
        paid = p.get("paid_hours")
        gross = p.get("gross_salary")
        expected_salary = round(work * hourly_rate, 2) if hourly_rate is not None else None
        salary_diff = round((gross - expected_salary), 2) if gross is not None and expected_salary is not None else None
        owed = round(abs(salary_diff), 2) if salary_diff is not None else None
        warnings: List[str] = []
        if paid is None:
            warnings.append("Ingen betalte timer fundet på lønseddel")
        elif abs(paid - work) > 0.25:
            warnings.append(f"Timeafvigelse: {paid:.2f} betalt vs {work:.2f} registreret")
        if p.get("sick_days") is None and sick_hours > 0:
            warnings.append("Ingen sygedage fundet på lønseddel")
        if p.get("vacation_days") is None and vacation_hours > 0:
            warnings.append("Ingen feriedage fundet på lønseddel")
        if salary_diff is not None and abs(salary_diff) > 50:
            warnings.append(f"Bruttoløn afviger: {gross:.2f} vs forventet {expected_salary:.2f}")
        rows.append({
            "period": period,
            "minuba_work_hours": work,
            "minuba_sick_hours": sick_hours,
            "minuba_vacation_hours": vacation_hours,
            "payslip_paid_hours": paid,
            "payslip_gross_salary": gross,
            "payslip_net_salary": p.get("net_salary"),
            "pension_employee": p.get("pension_employee"),
            "pension_employer": p.get("pension_employer"),
            "payslip_sick_days": p.get("sick_days"),
            "payslip_vacation_days": p.get("vacation_days"),
            "expected_salary": expected_salary,
            "salary_diff": salary_diff,
            "owed_amount": owed,
            "warnings": " | ".join(warnings),
        })
    return rows


def write_csv(rows: List[dict], path: Path):
    if not rows:
        return
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def write_json(rows: List[dict], path: Path):
    path.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")


def make_pdf(rows: List[dict], path: Path, hourly_rate: Optional[float]):
    pdf = FPDF()
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.add_page()
    pdf.set_font("Helvetica", "B", 16)
    pdf.cell(0, 10, "Loencheck rapport", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.ln(2)
    pdf.set_font("Helvetica", "", 10)
    pdf.cell(0, 6, f"Timeloen: {hourly_rate if hourly_rate is not None else 'ikke angivet'}", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.ln(2)

    total_expected = sum((r["expected_salary"] or 0.0) for r in rows)
    total_paid = sum((r["payslip_gross_salary"] or 0.0) for r in rows)
    total_diff = sum((r["salary_diff"] or 0.0) for r in rows)

    pdf.set_font("Helvetica", "B", 11)
    pdf.cell(0, 8, "Samlet oversigt", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.set_font("Helvetica", "", 10)
    pdf.cell(0, 6, f"Forventet bruttoloen: {total_expected:,.2f}".replace(",", "X").replace(".", ",").replace("X", "."), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.cell(0, 6, f"Faktisk bruttoloen: {total_paid:,.2f}".replace(",", "X").replace(".", ",").replace("X", "."), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.cell(0, 6, f"Difference / evt skyldig: {total_diff:,.2f}".replace(",", "X").replace(".", ",").replace("X", "."), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.ln(4)

    pdf.set_font("Helvetica", "B", 10)
    widths = [18, 24, 24, 24, 24, 24, 30]
    headers = ["Maaned", "Timer", "Loen", "Forv.", "Diff.", "Feriedag.", "Advarsel"]
    for w, h in zip(widths, headers):
        pdf.cell(w, 8, h, border=1)
    pdf.ln()

    pdf.set_font("Helvetica", "", 9)
    for r in rows:
        warn = r["warnings"][:40] if r["warnings"] else "-"
        cells = [
            r["period"],
            f"{r['payslip_paid_hours']:.2f}" if r["payslip_paid_hours"] is not None else "-",
            f"{r['payslip_gross_salary']:.2f}" if r["payslip_gross_salary"] is not None else "-",
            f"{r['expected_salary']:.2f}" if r["expected_salary"] is not None else "-",
            f"{r['salary_diff']:.2f}" if r["salary_diff"] is not None else "-",
            f"{r['payslip_vacation_days']:.2f}" if r["payslip_vacation_days"] is not None else "-",
            warn,
        ]
        for i, cell in enumerate(cells):
            pdf.cell(widths[i], 8, str(cell), border=1)
        pdf.ln()

    pdf.ln(4)
    pdf.set_font("Helvetica", "B", 11)
    pdf.cell(0, 8, "Maaneder med afvigelser", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.set_font("Helvetica", "", 9)
    for r in rows:
        if r["warnings"]:
            owed = r["owed_amount"] if r["owed_amount"] is not None else 0.0
            pdf.set_x(10)
            pdf.multi_cell(180, 5, f"{r['period']}: evt skyldig {owed:.2f} | {r['warnings']}")

    path.parent.mkdir(parents=True, exist_ok=True)
    pdf.output(str(path))


def main():
    parser = argparse.ArgumentParser(description="Sammenlign Minuba-timer med loensedler")
    parser.add_argument("--minuba-csv", required=True)
    parser.add_argument("--payslip-csv", required=True)
    parser.add_argument("--hourly-rate", type=float, default=None)
    parser.add_argument("--out-csv", default="reconciliation_report.csv")
    parser.add_argument("--out-json", default="reconciliation_report.json")
    parser.add_argument("--out-pdf", default="reconciliation_report.pdf")
    args = parser.parse_args()

    minuba = load_minuba_csv(Path(args.minuba_csv))
    payslips = load_payslips_csv(Path(args.payslip_csv))
    rows = reconcile(minuba, payslips, args.hourly_rate)

    write_csv(rows, Path(args.out_csv))
    write_json(rows, Path(args.out_json))
    if args.hourly_rate is not None:
        make_pdf(rows, Path(args.out_pdf), args.hourly_rate)

    print("Færdig.")
    print(f"Perioder behandlet: {len(rows)}")
    print(f"CSV gemt: {args.out_csv}")
    print(f"JSON gemt: {args.out_json}")
    if args.hourly_rate is not None:
        print(f"PDF gemt: {args.out_pdf}")
    flagged = [r for r in rows if r['warnings']]
    print(f"Perioder med advarsler: {len(flagged)}")
    for row in flagged[:12]:
        print(f" - {row['period']}: {row['warnings']}")


if __name__ == "__main__":
    main()