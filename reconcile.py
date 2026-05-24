#!/usr/bin/env python3
import argparse
import csv
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

from fpdf import FPDF
from fpdf.enums import XPos, YPos


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


def parse_date(value) -> Optional[datetime]:
    if not value:
        return None
    text = str(value).strip()
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(text[:10], fmt)
        except Exception:
            pass
    return None


def is_friday(date_obj: datetime) -> bool:
    return date_obj.weekday() == 4


def sick_hours_from_days(days: float, date_obj: Optional[datetime]) -> float:
    return days * (7.0 if date_obj and is_friday(date_obj) else 7.5)


def detect_category(row) -> str:
    category = (row.get("category") or row.get("type") or row.get("kategori") or "").strip().lower()
    note = (row.get("note") or row.get("tekst") or row.get("description") or "").lower()
    if category:
        if category in ("sick", "sygdom", "syg"):
            return "sick"
        if category in ("vacation", "ferie"):
            return "vacation"
        return "work"
    if any(k in note for k in ["syg", "sygdom", "fravær"]):
        return "sick"
    if any(k in note for k in ["ferie", "vacation"]):
        return "vacation"
    return "work"


def load_minuba_csv(path: Path):
    total = {"work_hours": 0.0, "sick_hours": 0.0, "vacation_hours": 0.0}
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            hours = normalize_decimal(row.get("hours") or row.get("timer") or row.get("antal_timer") or 0) or 0.0
            cat = detect_category(row)
            if cat == "sick":
                total["sick_hours"] += hours
            elif cat == "vacation":
                total["vacation_hours"] += hours
            else:
                total["work_hours"] += hours
    return total


def load_payslip_csv(path: Path):
    result = {
        "work_hours": 0.0,
        "sick_hours": 0.0,
        "gross_salary": 0.0,
        "net_salary": 0.0,
        "sh_amount": 0.0,
        "other_deductions": 0.0,
        "rows": [],
    }
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            result["rows"].append(row)
            work = normalize_decimal(
                row.get("arbejdstimer") or row.get("hours") or row.get("timer") or 0
            ) or 0.0
            result["work_hours"] += work

            sick_days = normalize_decimal(row.get("sygdom_days") or row.get("sygedage") or row.get("sick_days") or 0) or 0.0
            sick_date = parse_date(row.get("date") or row.get("dato") or row.get("day") or row.get("Date"))
            if sick_days:
                result["sick_hours"] += sick_hours_from_days(sick_days, sick_date)

            gross = normalize_decimal(row.get("brutto") or row.get("gross") or 0) or 0.0
            net = normalize_decimal(row.get("netto") or row.get("net") or 0) or 0.0
            sh = normalize_decimal(row.get("sh") or row.get("sh_amount") or row.get("savings") or row.get("opsparing") or 0) or 0.0
            other = normalize_decimal(row.get("andre_fradrag") or row.get("other_deductions") or 0) or 0.0

            result["gross_salary"] += gross
            result["net_salary"] += net
            result["sh_amount"] += sh
            result["other_deductions"] += other
    return result


def reconcile(minuba, payslip, hourly_rate):
    minuba_total = round(minuba["work_hours"] + minuba["sick_hours"], 2)
    payslip_total = round(payslip["work_hours"] + payslip["sick_hours"], 2)
    hour_diff = round(minuba_total - payslip_total, 2)
    money_diff = round(hour_diff * hourly_rate, 2)
    expected_gross = round(payslip_total * hourly_rate, 2)
    gross_diff = round(payslip["gross_salary"] - expected_gross, 2)

    if hour_diff > 0:
        status = "De skylder dig"
        conclusion = (
            f"Minuba viser {minuba_total:.2f} betalte timer i alt, mens lønsedlen viser {payslip_total:.2f}. "
            f"Der mangler derfor {hour_diff:.2f} timer, svarende til {money_diff:.2f} kr. før skat og fradrag."
        )
    elif hour_diff < 0:
        status = "Du skylder dem"
        conclusion = (
            f"Lønsedlen viser {payslip_total:.2f} betalte timer i alt, mens Minuba viser {minuba_total:.2f}. "
            f"Der er derfor {abs(hour_diff):.2f} timer for meget, svarende til {abs(money_diff):.2f} kr. før skat og fradrag."
        )
    else:
        status = "Timerne matcher"
        conclusion = f"Minuba og lønsedlens betalte timer matcher præcist: {minuba_total:.2f} timer."

    summary = (
        f"Minuba arbejdstimer: {minuba['work_hours']:.2f}. "
        f"Minuba sygetimer: {minuba['sick_hours']:.2f}. "
        f"Ferie-timer er ikke medregnet. "
        f"Lønsedlens arbejdstimer: {payslip['work_hours']:.2f}. "
        f"Lønsedlens sygetimer beregnet fra dage: {payslip['sick_hours']:.2f}. "
        f"Bruttoløn afviger med {gross_diff:.2f} kr. i forhold til timeløn-beregning."
    )

    return {
        "status": status,
        "minuba_work_hours": round(minuba["work_hours"], 2),
        "minuba_sick_hours": round(minuba["sick_hours"], 2),
        "minuba_total_hours": minuba_total,
        "payslip_work_hours": round(payslip["work_hours"], 2),
        "payslip_sick_hours": round(payslip["sick_hours"], 2),
        "payslip_total_hours": payslip_total,
        "hour_diff": hour_diff,
        "money_diff": money_diff,
        "expected_gross": expected_gross,
        "gross_salary": round(payslip["gross_salary"], 2),
        "gross_diff": gross_diff,
        "net_salary": round(payslip["net_salary"], 2),
        "sh_amount": round(payslip["sh_amount"], 2),
        "other_deductions": round(payslip["other_deductions"], 2),
        "summary": summary,
        "conclusion": conclusion,
    }


def money(x):
    return f"{x:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def write_csv(data, path: Path):
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(data.keys()))
        writer.writeheader()
        writer.writerow(data)


def write_json(data, path: Path):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def make_pdf(data, path: Path, hourly_rate: float):
    pdf = FPDF()
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.add_page()

    pdf.set_font("Helvetica", "B", 18)
    pdf.cell(0, 10, "Aarsbaseret loencheck", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.ln(2)

    pdf.set_font("Helvetica", "", 10)
    pdf.multi_cell(
        0,
        6,
        "Denne rapport sammenligner arbejdstimer og sygetimer fra Minuba med de samlede betalte timer på lønsedlerne. "
        "Ferie-timer er ikke medregnet, da de er selvbetalte. Sygedage i lønsedlen omregnes til timer, så de kan sammenlignes med Minubas timeregistrering."
    )
    pdf.ln(2)

    if data["hour_diff"] > 0:
        pdf.set_fill_color(70, 170, 70)
        headline = f"De skylder dig: {money(data['money_diff'])} kr."
        txt = (255, 255, 255)
    elif data["hour_diff"] < 0:
        pdf.set_fill_color(255, 70, 70)
        headline = f"Du skylder dem: {money(abs(data['money_diff']))} kr."
        txt = (255, 255, 255)
    else:
        pdf.set_fill_color(220, 220, 220)
        headline = "Timerne matcher"
        txt = (0, 0, 0)

    pdf.set_text_color(*txt)
    pdf.set_font("Helvetica", "B", 22)
    pdf.cell(0, 18, headline, border=1, new_x=XPos.LMARGIN, new_y=YPos.NEXT, align="C", fill=True)
    pdf.set_text_color(0, 0, 0)
    pdf.ln(3)

    pdf.set_font("Helvetica", "B", 12)
    pdf.cell(0, 8, "Sammenligning", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.set_font("Helvetica", "", 10)
    pdf.cell(0, 6, f"Minuba arbejdstimer: {money(data['minuba_work_hours'])}", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.cell(0, 6, f"Minuba sygetimer: {money(data['minuba_sick_hours'])}", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.cell(0, 6, f"Minuba total betalte timer: {money(data['minuba_total_hours'])}", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.cell(0, 6, f"Lønseddel arbejdstimer: {money(data['payslip_work_hours'])}", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.cell(0, 6, f"Lønseddel sygetimer: {money(data['payslip_sick_hours'])}", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.cell(0, 6, f"Lønseddel total betalte timer: {money(data['payslip_total_hours'])}", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.cell(0, 6, f"Forskel i timer: {money(data['hour_diff'])}", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.cell(0, 6, f"Timeloen: {money(hourly_rate)} kr.", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.cell(0, 6, f"Forventet bruttoloen: {money(data['expected_gross'])}", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.cell(0, 6, f"Faktisk bruttoloen: {money(data['gross_salary'])}", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.cell(0, 6, f"SH/opsparing: {money(data['sh_amount'])} | Andre fradrag: {money(data['other_deductions'])}", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.ln(3)

    pdf.set_font("Helvetica", "B", 12)
    pdf.cell(0, 8, "Forklaring", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.set_font("Helvetica", "", 10)
    pdf.multi_cell(
        0,
        6,
        "Reglen er: Hvis lønsedlens samlede betalte timer er højere end Minubas samlede betalte timer, så er der betalt for meget. "
        "Hvis Minuba viser flere betalte timer end lønsedlen, så mangler der betaling. Ferie er udeladt, mens sygdom er medregnet via en omregning fra dage til timer."
    )
    pdf.ln(2)

    pdf.set_font("Helvetica", "B", 12)
    pdf.cell(0, 8, "Resumé til HR", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.set_font("Helvetica", "", 10)
    pdf.multi_cell(0, 6, data["summary"])

    pdf.ln(2)
    pdf.set_font("Helvetica", "B", 12)
    pdf.cell(0, 8, "Konklusion", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.set_font("Helvetica", "", 10)
    pdf.multi_cell(0, 6, data["conclusion"])

    path.parent.mkdir(parents=True, exist_ok=True)
    pdf.output(str(path))


def main():
    parser = argparse.ArgumentParser(description="Aarsbaseret loencheck")
    parser.add_argument("--minuba-csv", required=True)
    parser.add_argument("--payslip-csv", required=True)
    parser.add_argument("--hourly-rate", type=float, required=True)
    parser.add_argument("--out-csv", default="annual_reconciliation_report.csv")
    parser.add_argument("--out-json", default="annual_reconciliation_report.json")
    parser.add_argument("--out-pdf", default="annual_reconciliation_report.pdf")
    args = parser.parse_args()

    minuba = load_minuba_csv(Path(args.minuba_csv))
    payslip = load_payslip_csv(Path(args.payslip_csv))
    data = reconcile(minuba, payslip, args.hourly_rate)

    write_csv(data, Path(args.out_csv))
    write_json(data, Path(args.out_json))
    make_pdf(data, Path(args.out_pdf), args.hourly_rate)

    print("Færdig.")
    print(f"CSV gemt: {args.out_csv}")
    print(f"JSON gemt: {args.out_json}")
    print(f"PDF gemt: {args.out_pdf}")
    print(f"Minuba total betalte timer: {data['minuba_total_hours']:.2f}")
    print(f"Lønseddel total betalte timer: {data['payslip_total_hours']:.2f}")
    print(f"Forskel timer: {data['hour_diff']:.2f}")
    print(f"Forskel kroner: {data['money_diff']:.2f}")


if __name__ == "__main__":
    main()