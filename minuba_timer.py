#!/usr/bin/env python3
"""
Minuba Timer Scraper
- Henter timer fra Minuba > Min Tid
- Klassificerer Arbejde / Ferie / Sygdom
- Eksporterer CSV og fuld PDF-rapport
- Direkte startdato i datofelt med fallback-navigation
- Egnet til GUI + PyInstaller
"""

import sys
import time
import re
import csv
import argparse
import getpass
from pathlib import Path
from datetime import date, datetime, timedelta
from collections import defaultdict

try:
    import pandas as pd
except ImportError:
    print("❌ Mangler pandas. Installer med: pip install pandas")
    sys.exit(1)

try:
    from fpdf import FPDF
except ImportError:
    print("❌ Mangler fpdf2. Installer med: pip install fpdf2")
    sys.exit(1)

try:
    from selenium import webdriver
    from selenium.webdriver.common.by import By
    from selenium.webdriver.common.keys import Keys
    from selenium.webdriver.chrome.service import Service
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.common.exceptions import TimeoutException, ElementNotInteractableException
except ImportError:
    print("❌ Mangler selenium. Installer med: pip install selenium webdriver-manager")
    sys.exit(1)

try:
    from webdriver_manager.chrome import ChromeDriverManager
except ImportError:
    print("❌ Mangler webdriver-manager. Installer med: pip install webdriver-manager")
    sys.exit(1)

FERIE_KEYWORDS = ["ferie", "vacation", "afspadsering", "feriefri", "holiday", "fravær"]
SYGDOM_KEYWORDS = ["syg", "sygdom", "sick", "barn syg", "barns sygdom", "omsorgsdage"]
MINUBA_URL = "https://app.minuba.dk"

RE_PAREN_ENTRY = re.compile(r"\(\s*([0-9]+[.,][0-9]+|[0-9]+)\s*\)\s*(?:(\d{1,2}:\d{2})\s*[-–]\s*(\d{1,2}:\d{2}))?")
RE_REG_TIMER = re.compile(r"registrerede\s+timer\s+([0-9]+[.,][0-9]+|[0-9]+)", re.IGNORECASE)
RE_FRAVAER = re.compile(r"frav[æa]r\s+([0-9]+[.,][0-9]+|[0-9]+)", re.IGNORECASE)
RE_SHORT_DATE = re.compile(r"(?:ma|ti|on|to|fr|lø|sø)\s+(\d{1,2}-\d{1,2})", re.IGNORECASE)
RE_FULL_DATE = re.compile(r"(\d{1,2}[./-]\d{1,2}[./-]\d{2,4}|\d{4}[./-]\d{2}[./-]\d{2})")
RE_SALDO_DATE = re.compile(r"saldo\s+start\s*\(\s*(\d{2}-\d{2}-\d{4})\s*\)", re.IGNORECASE)
RE_CLOCK_COLUMN = re.compile(r"^(\d{2}:\d{2})\s*$")

DATE_INPUT_SELECTORS = [
    "input.z-textbox.hasDatepicker",
    "input.z-datebox-input",
    "input[class*='hasDatepicker']",
    "input[class*='z-textbox']",
    "input[class*='cal']",
    "input[type='date']",
    "input[id*='date']",
    "input[name*='date']",
]


def classify(text: str) -> str:
    t = str(text or "").lower()
    if any(kw in t for kw in FERIE_KEYWORDS):
        return "Ferie"
    if any(kw in t for kw in SYGDOM_KEYWORDS):
        return "Sygdom"
    return "Arbejde"


def parse_float(s: str) -> float:
    try:
        return float(str(s or "").strip().replace(",", "."))
    except Exception:
        return 0.0


def format_hours(hours: float) -> str:
    return f"{float(hours or 0):.2f}"


def parse_date_any(value: str, year_hint: int = None):
    s = str(value or "").strip()
    if not s:
        return None
    for fmt in ("%d-%m-%Y", "%Y-%m-%d", "%d/%m/%Y", "%d.%m.%Y", "%d-%m-%y"):
        try:
            d = datetime.strptime(s, fmt).date()
            if d.year < 100:
                d = d.replace(year=2000 + d.year)
            return d
        except Exception:
            pass
    if year_hint:
        m = re.match(r"^(\d{1,2})-(\d{1,2})$", s)
        if m:
            try:
                return date(year_hint, int(m.group(2)), int(m.group(1)))
            except Exception:
                pass
    try:
        d = pd.to_datetime(s, dayfirst=True, errors="coerce")
        if pd.notna(d):
            return d.date()
    except Exception:
        pass
    return None


def get_date_range(periode=None, fra=None, til=None):
    today = date.today()
    if periode:
        if "-Q" in periode:
            y, q = periode.split("-Q")
            start_m = (int(q) - 1) * 3 + 1
            start = pd.Timestamp(int(y), start_m, 1)
            end = start + pd.offsets.QuarterEnd(1)
        elif len(periode) == 7:
            start = pd.Timestamp(periode + "-01")
            end = start + pd.offsets.MonthEnd(1)
        elif len(periode) == 4:
            start = pd.Timestamp(int(periode), 1, 1)
            end = pd.Timestamp(int(periode), 12, 31)
        else:
            raise ValueError(f"Ukendt periodeformat: {periode}")
        return start.date(), end.date()
    if fra and til:
        return pd.Timestamp(fra).date(), pd.Timestamp(til).date()
    start = pd.Timestamp(today.year, today.month, 1)
    end = start + pd.offsets.MonthEnd(1)
    return start.date(), end.date()


def build_driver(headless=True):
    opts = Options()
    if headless:
        opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--window-size=1440,1000")
    opts.add_argument("--lang=da-DK")
    opts.add_experimental_option("excludeSwitches", ["enable-logging"])
    return webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=opts)


def write_debug(driver, prefix="debug"):
    try:
        with open(f"{prefix}.html", "w", encoding="utf-8") as f:
            f.write(driver.page_source)
        driver.save_screenshot(f"{prefix}.png")
        print(f" ℹ️ Gemt {prefix}.html og {prefix}.png")
    except Exception as e:
        print(f" ⚠️ Debug-filer fejlede: {e}")


def find_visible(driver, selectors):
    for css in selectors:
        try:
            for el in driver.find_elements(By.CSS_SELECTOR, css):
                if el.is_displayed() and el.is_enabled():
                    return el
        except Exception:
            pass
    return None


def interact(el, val):
    try:
        el.clear()
        el.send_keys(val)
        return True
    except ElementNotInteractableException:
        try:
            el._parent.execute_script(
                "arguments[0].value=arguments[1];"
                "arguments[0].dispatchEvent(new Event('input',{bubbles:true}));"
                "arguments[0].dispatchEvent(new Event('change',{bubbles:true}));",
                el, val,
            )
            return True
        except Exception:
            return False
    except Exception:
        return False


def click_by_text(driver, texts):
    wanted = [t.lower() for t in texts]
    for tag in ["button", "a", "span", "div", "li"]:
        try:
            for el in driver.find_elements(By.TAG_NAME, tag):
                if el.is_displayed() and el.is_enabled():
                    txt = el.text.strip().lower()
                    if txt and any(w in txt for w in wanted):
                        driver.execute_script("arguments[0].scrollIntoView({block:'center'}); arguments[0].click();", el)
                        return True
        except Exception:
            continue
    return False


def login(driver, email, adgangskode):
    print("🔐 Logger ind på Minuba...")
    wait = WebDriverWait(driver, 20)
    email_sel = ["input[type='email']", "input[name='email']", "input[name='username']", "input[id='username']", "input[id*='email']", "input[placeholder*='mail']"]
    pw_sel = ["input[type='password']", "input[name='password']", "input[id*='password']", "input[placeholder*='adgang']", "input[placeholder*='kode']"]
    btn_sel = ["button[type='submit']", "button.login-btn", "input[type='submit']"]

    email_felt = None
    for url in [f"{MINUBA_URL}/#/login", f"{MINUBA_URL}/login"]:
        driver.get(url)
        try:
            wait.until(lambda d: find_visible(d, email_sel) is not None)
            email_felt = find_visible(driver, email_sel)
            if email_felt:
                break
        except TimeoutException:
            continue

    if not email_felt:
        print("❌ Kunne ikke finde email-felt")
        write_debug(driver, "debug_login")
        driver.quit()
        sys.exit(1)

    interact(email_felt, email)
    kode_felt = find_visible(driver, pw_sel)
    if not kode_felt:
        print("❌ Kunne ikke finde adgangskode-felt")
        write_debug(driver, "debug_login")
        driver.quit()
        sys.exit(1)

    interact(kode_felt, adgangskode)
    btn = find_visible(driver, btn_sel)
    if not btn:
        try:
            btn = driver.find_element(By.XPATH, "//button[contains(translate(.,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'log ind') or contains(translate(.,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'login')]")
        except Exception:
            pass
    if not btn:
        print("❌ Kunne ikke finde login-knap")
        write_debug(driver, "debug_login")
        driver.quit()
        sys.exit(1)

    btn.click()
    try:
        wait.until(lambda d: "login" not in d.current_url.lower())
    except TimeoutException:
        time.sleep(3)
        if "login" in driver.current_url.lower():
            print("❌ Login mislykkedes — tjek email og adgangskode")
            write_debug(driver, "debug_login_failed")
            driver.quit()
            sys.exit(1)
    print("✅ Logget ind!")


def navigate_to_min_tid(driver):
    print("📋 Navigerer til Min Tid...")
    driver.get(f"{MINUBA_URL}/#/mytimeregistration")
    time.sleep(2)
    click_by_text(driver, ["Min tid", "MinTid", "Min Time", "My time"])
    wait = WebDriverWait(driver, 20)
    wait.until(lambda d: find_visible(d, DATE_INPUT_SELECTORS) is not None)
    print("✅ Min Tid er åben")


def get_cal_date(driver):
    for css in DATE_INPUT_SELECTORS:
        try:
            for el in driver.find_elements(By.CSS_SELECTOR, css):
                v = (el.get_attribute("value") or "").strip()
                if v:
                    d = parse_date_any(v)
                    if d:
                        return d
        except Exception:
            pass
    return None


def set_start_date(driver, target_date: date) -> bool:
    print(f"📅 Sætter startdato direkte til {target_date}...")
    value_iso = target_date.strftime("%Y-%m-%d")
    value_dk = target_date.strftime("%d-%m-%Y")
    value_dk_slash = target_date.strftime("%d/%m/%Y")

    for css in DATE_INPUT_SELECTORS:
        try:
            for el in driver.find_elements(By.CSS_SELECTOR, css):
                if not el.is_displayed():
                    continue

                driver.execute_script(
                    """
                    const el = arguments[0];
                    const values = [arguments[1], arguments[2], arguments[3]];
                    el.removeAttribute('readonly');
                    el.focus();
                    for (const v of values) {
                        try {
                            const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set;
                            if (setter) setter.call(el, v);
                            else el.value = v;
                            el.dispatchEvent(new Event('input', { bubbles: true }));
                            el.dispatchEvent(new Event('change', { bubbles: true }));
                            el.dispatchEvent(new Event('blur', { bubbles: true }));
                        } catch (e) {}
                    }
                    """,
                    el, value_dk, value_iso, value_dk_slash,
                )

                for candidate in [value_dk, value_iso, value_dk_slash]:
                    try:
                        el.send_keys(Keys.CONTROL, "a")
                        el.send_keys(candidate)
                        el.send_keys(Keys.TAB)
                        time.sleep(1.0)
                        current = (el.get_attribute("value") or "").strip()
                        parsed = parse_date_any(current)
                        if parsed == target_date:
                            print(f"✅ Startdato sat via felt: {current}")
                            return True
                    except Exception:
                        pass

                current = (el.get_attribute("value") or "").strip()
                parsed = parse_date_any(current)
                if parsed == target_date:
                    print(f"✅ Startdato sat via JS: {current}")
                    return True
        except Exception:
            pass

    print("⚠️ Kunne ikke sætte startdato direkte i feltet")
    return False


def click_next(driver) -> bool:
    selectors = [
        "div.arrowWrap[title*='Næste']", "div.arrowWrap[title*='Next']", "div.arrow-right",
        "button[title*='Næste']", "button[aria-label*='Næste']", "button[title*='Next']",
        "button[aria-label*='Next']", "a[title*='Næste']", "a[aria-label*='Næste']", "a[title*='Next']",
    ]
    for css in selectors:
        try:
            for btn in driver.find_elements(By.CSS_SELECTOR, css):
                if btn.is_displayed() and btn.is_enabled():
                    driver.execute_script("arguments[0].scrollIntoView({block:'center'}); arguments[0].click();", btn)
                    return True
        except Exception:
            continue
    return False


def click_prev(driver) -> bool:
    selectors = [
        "div.arrowWrap[title*='Forrige']", "div.arrowWrap[title*='Prev']", "button[title*='Forrige']",
        "button[aria-label*='Forrige']", "button[title*='Prev']", "button[aria-label*='Prev']",
        "a[title*='Forrige']", "a[aria-label*='Forrige']", "div.arrow-left",
    ]
    for css in selectors:
        try:
            for btn in driver.find_elements(By.CSS_SELECTOR, css):
                if btn.is_displayed() and btn.is_enabled():
                    driver.execute_script("arguments[0].scrollIntoView({block:'center'}); arguments[0].click();", btn)
                    return True
        except Exception:
            continue
    return False


def navigate_to_start_date_fallback(driver, target_date: date):
    print("↩️ Falder tilbage til kalender-navigation...")
    current = get_cal_date(driver)
    if not current:
        print("⚠️ Kunne ikke læse aktuel kalenderdato")
        return
    safety = 0
    while current and current > target_date and safety < 400:
        if not click_prev(driver):
            break
        time.sleep(0.5)
        new_date = get_cal_date(driver)
        if new_date == current:
            break
        current = new_date
        safety += 1
    print(f"📅 Kalender står nu på: {current}")


def parse_page_body(body_text: str, year_hint: int) -> list:
    lines = []
    for line in body_text.splitlines():
        l = line.strip()
        if not l:
            continue
        if RE_CLOCK_COLUMN.match(l):
            continue
        lines.append(l)

    saldo_info = {}
    for i, line in enumerate(lines):
        m = RE_SALDO_DATE.search(line)
        if not m:
            continue
        d = parse_date_any(m.group(1))
        if not d or d in saldo_info:
            continue
        reg_timer = 0.0
        fravaer_val = 0.0
        for j in range(i, min(i + 20, len(lines))):
            mr = RE_REG_TIMER.search(lines[j])
            if mr:
                reg_timer = parse_float(mr.group(1))
            mf = RE_FRAVAER.search(lines[j])
            if mf:
                fravaer_val = parse_float(mf.group(1))
        saldo_info[d] = {"reg_timer": reg_timer, "fravaer": fravaer_val}

    task_entries = []
    current_date = None
    for i, line in enumerate(lines):
        ms = RE_SHORT_DATE.search(line)
        if ms:
            d = parse_date_any(ms.group(1), year_hint=year_hint)
            if d:
                current_date = d
        mf = RE_FULL_DATE.search(line)
        if mf and not ms:
            d = parse_date_any(mf.group(1))
            if d and d.year > 2000:
                current_date = d
        mp = RE_PAREN_ENTRY.search(line)
        if mp and current_date:
            hours = parse_float(mp.group(1))
            if 0.1 <= hours <= 24.0:
                type_text = lines[i + 1].strip() if i + 1 < len(lines) else line
                task_entries.append({"date": current_date, "hours": hours, "type": type_text, "raw": line})

    entries = []
    seen_keys = set()
    for d, info in saldo_info.items():
        reg_timer = info["reg_timer"]
        fravaer_val = info["fravaer"]
        day_tasks = [t for t in task_entries if t["date"] == d]
        is_fravaer = fravaer_val > 0

        if is_fravaer:
            type_text = "Ferie"
            for t in day_tasks:
                c = classify(t["type"])
                if c != "Arbejde":
                    type_text = c
                    break
            total = reg_timer if reg_timer > 0 else fravaer_val
            if total > 0:
                key = (d, round(total, 2), type_text)
                if key not in seen_keys:
                    seen_keys.add(key)
                    entries.append({"date": d, "hours": total, "type": type_text, "raw": f"Fravær-dag: reg={reg_timer}, fravær={fravaer_val}"})

        elif day_tasks:
            task_sum = sum(t["hours"] for t in day_tasks)
            for t in day_tasks:
                key = (d, round(t["hours"], 2), t["type"][:40].lower())
                if key not in seen_keys:
                    seen_keys.add(key)
                    entries.append({"date": d, "hours": t["hours"], "type": t["type"], "raw": t["raw"]})
            if reg_timer > 0 and abs(task_sum - reg_timer) > 0.1:
                print(f" ⚠️ {d}: task-sum={task_sum:.2f}t ≠ reg_timer={reg_timer:.2f}t")

        elif reg_timer > 0:
            key = (d, round(reg_timer, 2), "arbejde")
            if key not in seen_keys:
                seen_keys.add(key)
                entries.append({"date": d, "hours": reg_timer, "type": "Arbejde", "raw": f"reg_timer={reg_timer} (ingen task-poster fundet)"})

    saldo_dates = set(saldo_info.keys())
    fallback_seen = set()
    for t in task_entries:
        if t["date"] not in saldo_dates:
            key = (t["date"], round(t["hours"], 2), t["type"][:40].lower())
            if key not in seen_keys and key not in fallback_seen:
                fallback_seen.add(key)
                entries.append(t)
    return entries


def scrape_time_entries(driver, fra: date, til: date) -> list:
    time.sleep(2)
    if not set_start_date(driver, fra):
        navigate_to_start_date_fallback(driver, fra)
    time.sleep(2)

    current_date = get_cal_date(driver) or fra
    print(f"📍 Starter scraping fra kalenderdato: {current_date}")

    all_entries = []
    seen_keys = set()
    max_steps = (til - fra).days + 10
    step = 0
    last_body = None
    no_change_count = 0

    while step < max_steps:
        try:
            body_text = driver.find_element(By.TAG_NAME, "body").text
        except Exception:
            body_text = ""

        if body_text == last_body:
            no_change_count += 1
            if no_change_count >= 3:
                print("⚠️ Siden ændrer sig ikke — stopper navigation")
                break
        else:
            no_change_count = 0
            last_body = body_text

        page_entries = parse_page_body(body_text, current_date.year)
        for e in page_entries:
            d = e.get("date")
            h = round(float(e.get("hours", 0) or 0), 2)
            t = str(e.get("type", "")).strip().lower()[:40]
            key = (d, h, t)
            if key not in seen_keys:
                seen_keys.add(key)
                all_entries.append(e)
                print(f"✅ {d}: {h}t ({classify(e.get('type', ''))}) — {str(e.get('type', ''))[:50]}")

        if current_date >= til:
            break

        if not click_next(driver):
            print("⚠️ Kunne ikke klikke Næste — stopper")
            break

        time.sleep(0.8)
        new_date = get_cal_date(driver)
        current_date = new_date if new_date and new_date != current_date else current_date + timedelta(days=1)
        step += 1

    print(f"\n✅ Navigation færdig — {len(all_entries)} poster fundet")
    return all_entries


def filter_entries_by_date(entries: list, fra: date, til: date) -> list:
    filtered = []
    seen_keys = set()
    for e in entries:
        d = e.get("date") if isinstance(e.get("date"), date) else parse_date_any(str(e.get("date") or ""))
        h = round(float(e.get("hours", 0) or 0), 2)
        t = str(e.get("type", "")).strip().lower()[:40]
        key = (d, h, t)
        if d is None or not (fra <= d <= til):
            continue
        if key in seen_keys:
            continue
        seen_keys.add(key)
        filtered.append(e)
    return sorted(filtered, key=lambda x: (x.get("date"), str(x.get("type") or "")))


def summarize_entries(entries: list):
    totals = defaultdict(float)
    monthly = defaultdict(lambda: defaultdict(float))
    details = []
    for e in entries:
        d = e.get("date") if isinstance(e.get("date"), date) else parse_date_any(str(e.get("date") or ""))
        hours = float(e.get("hours", 0) or 0)
        type_text = str(e.get("type") or "")
        category = classify(type_text)
        totals[category] += hours
        if d:
            monthly[d.strftime("%Y-%m")][category] += hours
            details.append({
                "date": d,
                "category": category,
                "hours": hours,
                "type": type_text,
                "raw": str(e.get("raw") or "")
            })
    return totals, monthly, sorted(details, key=lambda x: (x["date"], x["category"], x["type"]))


def print_rapport(entries: list, fra: date, til: date, vis_typer: bool = False):
    if not entries:
        print("\n⚠️ Ingen tidsregistreringer fundet.")
        return

    totals, monthly, _ = summarize_entries(entries)
    arbejde = totals.get("Arbejde", 0.0)
    ferie = totals.get("Ferie", 0.0)
    sygdom = totals.get("Sygdom", 0.0)
    total = arbejde + ferie + sygdom

    print("\n" + "═" * 62)
    print(" MINUBA TIMER OVERSIGT")
    print(f" Periode: {fra} → {til}")
    print("═" * 62)
    print(f" {'Arbejdstimer':<24} {arbejde:>8.2f} t")
    print(f" {'Ferie':<24} {ferie:>8.2f} t")
    print(f" {'Sygdom':<24} {sygdom:>8.2f} t")
    print(" " + "─" * 34)
    print(f" {'TOTAL':<24} {total:>8.2f} t")
    print("═" * 62)

    if len(monthly) > 1:
        print("\n MÅNEDSOPDELING:")
        print(" " + "─" * 58)
        print(f" {'Måned':<10} {'Arbejde':>10} {'Ferie':>10} {'Sygdom':>10} {'Total':>10}")
        print(" " + "─" * 58)
        for m in sorted(monthly.keys()):
            d2 = monthly[m]
            t2 = sum(d2.values())
            print(f" {m:<10} {d2.get('Arbejde',0):>10.2f} {d2.get('Ferie',0):>10.2f} {d2.get('Sygdom',0):>10.2f} {t2:>10.2f}")

    if vis_typer:
        type_totals = defaultdict(float)
        for e in entries:
            t3 = str(e.get("type") or "Ukendt")
            type_totals[t3] += float(e.get("hours", 0) or 0)
        print("\n ALLE TYPER FUNDET:")
        print(" " + "─" * 60)
        for t3, h in sorted(type_totals.items(), key=lambda x: -x[1]):
            print(f" [{classify(t3):8s}] {t3:<40} {h:>7.2f} t")


def export_csv(entries: list, csv_path: str):
    path = Path(csv_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f, delimiter=';')
        writer.writerow(["Dato", "Kategori", "Timer", "Type", "Rå tekst"])
        for e in entries:
            d = e.get("date")
            d_text = d.strftime("%Y-%m-%d") if isinstance(d, date) else str(d or "")
            type_text = str(e.get("type") or "")
            writer.writerow([d_text, classify(type_text), format_hours(e.get("hours", 0)), type_text, str(e.get("raw") or "")])


class ReportPDF(FPDF):
    def header(self):
        self.set_fill_color(21, 68, 94)
        self.rect(0, 0, 210, 22, style='F')
        self.set_text_color(255, 255, 255)
        self.set_font("Helvetica", "B", 16)
        self.set_xy(12, 7)
        self.cell(0, 8, "Minuba Løncheck Rapport")
        self.ln(18)
        self.set_text_color(0, 0, 0)

    def footer(self):
        self.set_y(-12)
        self.set_font("Helvetica", "I", 8)
        self.set_text_color(110, 110, 110)
        self.cell(0, 8, f"Side {self.page_no()}", align="C")


def pdf_section_title(pdf, title):
    pdf.ln(3)
    pdf.set_fill_color(230, 236, 242)
    pdf.set_draw_color(200, 210, 220)
    pdf.set_font("Helvetica", "B", 11)
    pdf.cell(0, 8, title, new_x="LMARGIN", new_y="NEXT", fill=True)
    pdf.ln(1)


def pdf_key_value(pdf, label, value, label_w=45):
    pdf.set_font("Helvetica", "B", 10)
    pdf.cell(label_w, 7, f"{label}:")
    pdf.set_font("Helvetica", "", 10)
    pdf.cell(0, 7, str(value), new_x="LMARGIN", new_y="NEXT")


def pdf_summary_table(pdf, totals, timeløn=None):
    arbejde = totals.get("Arbejde", 0.0)
    ferie = totals.get("Ferie", 0.0)
    sygdom = totals.get("Sygdom", 0.0)
    total = arbejde + ferie + sygdom
    rows = [
        ("Arbejdstimer", arbejde),
        ("Ferie", ferie),
        ("Sygdom", sygdom),
        ("Total", total),
    ]
    if timeløn is not None:
        try:
            løn = float(str(timeløn).replace(",", ".")) * arbejde
            rows.append(("Foreløbig løn (arbejde x timeløn)", løn, "kr"))
        except Exception:
            pass

    col1 = 105
    col2 = 35
    pdf.set_font("Helvetica", "B", 10)
    pdf.set_fill_color(240, 240, 240)
    pdf.cell(col1, 8, "Post", border=1, fill=True)
    pdf.cell(col2, 8, "Værdi", border=1, fill=True, align="R")
    pdf.cell(25, 8, "Enhed", border=1, fill=True, align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 10)
    for idx, row in enumerate(rows):
        label = row[0]
        value = row[1]
        unit = row[2] if len(row) > 2 else "timer"
        if idx % 2 == 0:
            pdf.set_fill_color(250, 250, 250)
            fill = True
        else:
            fill = False
        pdf.cell(col1, 8, label, border=1, fill=fill)
        pdf.cell(col2, 8, format_hours(value) if unit == "timer" else f"{value:.2f}", border=1, fill=fill, align="R")
        pdf.cell(25, 8, unit, border=1, fill=fill, align="C", new_x="LMARGIN", new_y="NEXT")


def pdf_monthly_table(pdf, monthly):
    if not monthly:
        return
    widths = [35, 35, 35, 35, 35]
    headers = ["Måned", "Arbejde", "Ferie", "Sygdom", "Total"]
    pdf.set_font("Helvetica", "B", 9)
    pdf.set_fill_color(240, 240, 240)
    for w, h in zip(widths, headers):
        pdf.cell(w, 8, h, border=1, fill=True, align="C")
    pdf.ln()
    pdf.set_font("Helvetica", "", 9)
    for i, month in enumerate(sorted(monthly.keys())):
        m = monthly[month]
        total = m.get("Arbejde", 0) + m.get("Ferie", 0) + m.get("Sygdom", 0)
        values = [month, format_hours(m.get("Arbejde", 0)), format_hours(m.get("Ferie", 0)), format_hours(m.get("Sygdom", 0)), format_hours(total)]
        if i % 2 == 0:
            pdf.set_fill_color(250, 250, 250)
            fill = True
        else:
            fill = False
        for w, val in zip(widths, values):
            align = "L" if w == widths[0] else "R"
            pdf.cell(w, 7, str(val), border=1, fill=fill, align=align)
        pdf.ln()


def pdf_detail_table(pdf, details):
    if not details:
        pdf.set_font("Helvetica", "", 10)
        pdf.multi_cell(0, 6, "Ingen poster fundet i valgt periode.")
        return

    widths = [24, 28, 20, 118]
    headers = ["Dato", "Kategori", "Timer", "Type / registrering"]

    def print_header():
        pdf.set_font("Helvetica", "B", 8)
        pdf.set_fill_color(240, 240, 240)
        for w, h in zip(widths, headers):
            align = "C" if h != "Type / registrering" else "L"
            pdf.cell(w, 8, h, border=1, fill=True, align=align)
        pdf.ln()

    print_header()
    pdf.set_font("Helvetica", "", 8)

    for i, row in enumerate(details):
        if pdf.get_y() > 268:
            pdf.add_page()
            print_header()
            pdf.set_font("Helvetica", "", 8)

        date_str = row["date"].strftime("%Y-%m-%d")
        category = row["category"]
        hours = format_hours(row["hours"])
        type_text = row["type"] or row["raw"] or "-"
        if len(type_text) > 72:
            type_text = type_text[:69] + "..."

        if i % 2 == 0:
            pdf.set_fill_color(250, 250, 250)
            fill = True
        else:
            fill = False

        line_h = 7
        pdf.cell(widths[0], line_h, date_str, border=1, fill=fill)
        pdf.cell(widths[1], line_h, category, border=1, fill=fill)
        pdf.cell(widths[2], line_h, hours, border=1, fill=fill, align="R")
        pdf.multi_cell(widths[3], line_h, type_text, border=1, fill=fill)


def generate_pdf_report(pdf_path, entries, fra, til, navn="", virksomhed="", timeløn=None):
    path = Path(pdf_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    totals, monthly, details = summarize_entries(entries)

    pdf = ReportPDF("P", "mm", "A4")
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.add_page()

    pdf.set_font("Helvetica", "", 10)
    pdf_key_value(pdf, "Periode", f"{fra} til {til}")
    if navn:
        pdf_key_value(pdf, "Navn", navn)
    if virksomhed:
        pdf_key_value(pdf, "Virksomhed", virksomhed)
    pdf_key_value(pdf, "Genereret", datetime.now().strftime("%Y-%m-%d %H:%M"))

    pdf_section_title(pdf, "Oversigt")
    pdf_summary_table(pdf, totals, timeløn=timeløn)

    pdf_section_title(pdf, "Månedsopdeling")
    pdf_monthly_table(pdf, monthly)

    pdf_section_title(pdf, "Detaljerede registreringer")
    pdf_detail_table(pdf, details)

    pdf.ln(4)
    pdf.set_font("Helvetica", "I", 9)
    pdf.set_text_color(90, 90, 90)
    pdf.multi_cell(
        0,
        5,
        "Bemærk: Rapporten er baseret på registreringer fra Minuba 'Min Tid' og er tænkt som kontrolgrundlag. "
        "Tallene bør sammenholdes med lønseddel, eventuelle tillæg, overtid, pension, SH/feriefridage og gældende overenskomst ved et egentligt løncheck."
    )
    pdf.output(str(path))


def main():
    parser = argparse.ArgumentParser(description="Henter timer fra Minuba (Min Tid).")
    parser.add_argument("--email", required=False)
    parser.add_argument("--adgangskode", required=False)
    parser.add_argument("--periode", default=None)
    parser.add_argument("--fra", default=None)
    parser.add_argument("--til", default=None)
    parser.add_argument("--navn", default="")
    parser.add_argument("--virksomhed", default="")
    parser.add_argument("--timeløn", default=None)
    parser.add_argument("--pdf", default=None)
    parser.add_argument("--csv", default=None)
    parser.add_argument("--no-headless", action="store_true")
    parser.add_argument("--vis-typer", action="store_true")
    args = parser.parse_args()

    if not args.email:
        args.email = input("Email: ").strip()
    if not args.adgangskode:
        args.adgangskode = getpass.getpass("Adgangskode: ").strip()
    if not args.email or not args.adgangskode:
        print("❌ Email og adgangskode er påkrævet")
        return 1

    fra, til = get_date_range(args.periode, args.fra, args.til)
    print(f"\n📅 Periode: {fra} → {til}")

    driver = build_driver(headless=not args.no_headless)
    try:
        login(driver, args.email, args.adgangskode)
        navigate_to_min_tid(driver)
        print("🔍 Henter registreringer...")
        entries = scrape_time_entries(driver, fra, til)
        if entries:
            entries = filter_entries_by_date(entries, fra, til)
            print(f" → {len(entries)} poster i perioden.")
        print_rapport(entries, fra, til, vis_typer=args.vis_typer)
        if args.csv:
            export_csv(entries, args.csv)
            print(f"📊 CSV gemt: {args.csv}")
        if args.pdf:
            generate_pdf_report(args.pdf, entries, fra, til, navn=args.navn, virksomhed=args.virksomhed, timeløn=args.timeløn)
            print(f"📄 PDF gemt: {args.pdf}")
    finally:
        driver.quit()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
