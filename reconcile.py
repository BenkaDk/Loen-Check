#!/usr/bin/env python3
import argparse
import csv
import json
import re
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

DATE_FORMATS = ["%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%Y/%m/%d"]
SICK_KEYWORDS = ["syg", "sygdom", "sygedag", "sygetimer"]
VACATION_KEYWORDS = ["ferie", "ferietimer", "feriedag"]


def parse_date(value: str) -> Optional[datetime]:
    value = (value or "").strip()
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    return None


def normalize_decimal(value) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return None
    text = text.replace("kr.", "").replace("kr", "")
    text = text.replace(" ", "")
    if text.count(",") == 1 and text.count(".") >= 1:
        text = text.replace(".", "").replace(",", ".")
    else:
        text = text.replace(",", ".")
    m = re.search(r"-?\d+(?:\.\d+)?", text)
    return float(m.group()) if m else None


def month_key(dt: datetime) -> str:
    return dt.strftime("%Y-%m")


def infer_category(text: str) -> str:
    low = (text or "").lower()
    if any(k in low for k in SICK_KEYWORDS):
        return "sick"
    if any(k in low for k in VACATION_KEYWORDS):
        return "vacation"
    return "work"


def load_minuba_csv(path: Path) -> Dict[str, dict]:
    months = defaultdict(lambda: {
        "work_hours": 0.0,
        "sick_hours": 0.0,
        "vacation_hours": 0.0,
        "entries": []
    })
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            date_raw = row.get("date") or row.get("dato") or row.get("day") or row.get("Date")
            dt = parse_date(date_raw or "")
            if not dt:
                continue
            hours = normalize_decimal(row.get("hours") or row.get("timer") or row.get("antal_timer") or 0) or 0.0
            category = (row.get("category") or row.get("type") or row.get("kategori") or "").strip().lower()
            note = row.get("note") or row.get("tekst") or row.get("description") or ""
            if not category:
                category = infer_category(note)
            key = month_key(dt)
            if category in ("sick", "sygdom", "syg"):
                months[key]["sick_hours"] += hours
            elif category in ("vacation", "ferie"):
                months[key]["vacation_hours"] += hours
            else:
                months[key]["work_hours"] += hours
            months[key]["entries"].append({
                "date": dt.strftime("%Y-%m-%d"),
                "hours": hours,
                "category": category,
                "note": note,
            })
    return months


def load_payslips_csv(path: Path) -> Dict[str, dict]:
    months = defaultdict(lambda: {
        "paid_hours": None,
        "gross_salary": None,
        "net_salary": None,
        "pension_employee": None,
        "pension_employer": None,
        "vacation_hours_paid": None,
        "sick_hours_paid": None,
        "lines": []
    })
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            period = row.get("period") or row.get("måned") or row.get("month") or row.get("periode")
            if not period:
                start = parse_date(row.get("period_start") or row.get("fra") or "")
                period = month_key(start) if start else None
            if not period:
                continue
            period = period[:7]
            rec = months[period]
            for src, dst in [
                ("paid_hours", "paid_hours"),
                ("gross_salary", "gross_salary"),
                ("net_salary", "net_salary"),
                ("pension_employee", "pension_employee"),
                ("pension_employer", "pension_employer"),
                ("vacation_hours_paid", "vacation_hours_paid"),
                ("sick_hours_paid", "sick_hours_paid"),
                ("arbejdstimer", "paid_hours"),
                ("brutto", "gross_salary"),
                ("netto", "net_salary"),
            ]:
                if rec[dst] is None:
                    val = normalize_decimal(row.get(src))
                    if val is not None:
                        rec[dst] = val
            rec["lines"].append(row)
    return months


def reconcile(minuba: Dict[str, dict], payslips: Dict[str, dict], hourly_rate: Optional[float]):
    periods = sorted(set(minuba) | set(payslips))
    rows = []
    for period in periods:
        m = minuba.get(period, {})
        p = payslips.get(period, {})
        work = round(m.get("work_hours", 0.0), 2)
        sick = round(m.get("sick_hours", 0.0), 2)
        vacation = round(m.get("vacation_hours", 0.0), 2)
        paid = p.get("paid_hours")
        gross = p.get("gross_salary")
        hour_diff = round((paid - work), 2) if paid is not None else None
        expected_salary = round(work * hourly_rate, 2) if hourly_rate is not None else None
        salary_diff = round((gross - expected_salary), 2) if gross is not None and expected_salary is not None else None
        warnings: List[str] = []
        if paid is None:
            warnings.append("Ingen betalte timer fundet på lønseddel")
        elif abs(paid - work) > 0.25:
            warnings.append(f"Timeafvigelse: {paid:.2f} betalt vs {work:.2f} registreret")
        vac_paid = p.get("vacation_hours_paid")
        if vac_paid is not None and abs(vac_paid - vacation) > 0.25:
            warnings.append(f"Ferie afviger: {vac_paid:.2f} på lønseddel vs {vacation:.2f} i Minuba")
        sick_paid = p.get("sick_hours_paid")
        if sick_paid is not None and abs(sick_paid - sick) > 0.25:
            warnings.append(f"Sygdom afviger: {sick_paid:.2f} på lønseddel vs {sick:.2f} i Minuba")
        if salary_diff is not None and abs(salary_diff) > 1.0:
            warnings.append(f"Bruttoløn afviger: {gross:.2f} vs forventet {expected_salary:.2f}")
        rows.append({
            "period": period,
            "minuba_work_hours": work,
            "minuba_sick_hours": sick,
            "minuba_vacation_hours": vacation,
            "payslip_paid_hours": paid,
            "payslip_gross_salary": gross,
            "payslip_net_salary": p.get("net_salary"),
            "pension_employee": p.get("pension_employee"),
            "pension_employer": p.get("pension_employer"),
            "expected_salary": expected_salary,
            "hour_diff": hour_diff,
            "salary_diff": salary_diff,
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


def main():
    parser = argparse.ArgumentParser(description="Sammenlign Minuba-timer med lønsedler")
    parser.add_argument("--minuba-csv", required=True, help="CSV fra dit Minuba-script")
    parser.add_argument("--payslip-csv", required=True, help="CSV fra dit lønseddel-script")
    parser.add_argument("--hourly-rate", type=float, default=None, help="Timeløn til forventet bruttoløn")
    parser.add_argument("--out-csv", default="reconciliation_report.csv")
    parser.add_argument("--out-json", default="reconciliation_report.json")
    args = parser.parse_args()

    minuba = load_minuba_csv(Path(args.minuba_csv))
    payslips = load_payslips_csv(Path(args.payslip_csv))
    rows = reconcile(minuba, payslips, args.hourly_rate)

    write_csv(rows, Path(args.out_csv))
    write_json(rows, Path(args.out_json))

    print("Færdig.")
    print(f"Perioder behandlet: {len(rows)}")
    print(f"CSV gemt: {args.out_csv}")
    print(f"JSON gemt: {args.out_json}")
    flagged = [r for r in rows if r["warnings"]]
    print(f"Perioder med advarsler: {len(flagged)}")
    for row in flagged[:12]:
        print(f" - {row['period']}: {row['warnings']}")


if __name__ == "__main__":
    main()