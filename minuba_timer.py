#!/usr/bin/env python3
"""
Minuba Timer Scraper + PDF-rapport
Bygget til at bevare den gamle login/scrape-adfærd og tilføje PDF-output.

KRAV (Arch anbefalet):
  sudo pacman -S chromium python-selenium python-pandas python-weasyprint

Alternativ pip:
  pip install selenium webdriver-manager pandas weasyprint openpyxl

BRUG:
  python minuba_timer.py --email dig@example.dk --adgangskode DinKode --periode 2025-04 --pdf april.pdf
  python minuba_timer.py --email dig@example.dk --adgangskode DinKode --fra 2025-01-01 --til 2025-03-31 --pdf q1.pdf
"""

import sys, time, re, argparse, getpass, json
from datetime import date, datetime, timedelta
from collections import defaultdict
from pathlib import Path

try:
    import pandas as pd
except ImportError:
    print("❌ pip install selenium webdriver-manager pandas weasyprint openpyxl")
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
    print("❌ pip install selenium webdriver-manager")
    sys.exit(1)

try:
    from webdriver_manager.chrome import ChromeDriverManager
except ImportError:
    ChromeDriverManager = None

try:
    from weasyprint import HTML
except ImportError:
    HTML = None

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


def parse_date_any(value: str, year_hint: int = None):
    s = str(value or "").strip()
    if not s:
        return None
    for fmt in ("%d-%m-%Y", "%Y-%m-%d", "%d/%m/%Y", "%d.%m.%Y"):
        try:
            return datetime.strptime(s, fmt).date()
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
    opts.add_argument("--window-size=1400,900")
    opts.add_argument("--lang=da-DK")
    opts.add_experimental_option("excludeSwitches", ["enable-logging"])

    chromium_bin = "/usr/bin/chromium"
    chromedriver_bin = "/usr/bin/chromedriver"
    if Path(chromium_bin).exists() and Path(chromedriver_bin).exists():
        opts.binary_location = chromium_bin
        return webdriver.Chrome(service=Service(chromedriver_bin), options=opts)

    if ChromeDriverManager is not None:
        return webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=opts)

    return webdriver.Chrome(options=opts)


def write_debug(driver, prefix="debug"):
    try:
        with open(f"{prefix}.html", "w", encoding="utf-8") as f:
            f.write(driver.page_source)
        driver.save_screenshot(f"{prefix}.png")
        print(f"   ℹ️ Gemt {prefix}.html og {prefix}.png")
    except Exception as e:
        print(f"   ⚠️ Debug-filer fejlede: {e}")


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
                el, val)
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
    email_sel = [
        "input[type='email']", "input[name='email']", "input[name='username']",
        "input[id='username']", "input[id*='email']", "input[placeholder*='mail']"
    ]
    pw_sel = [
        "input[type='password']", "input[name='password']", "input[id*='password']",
        "input[placeholder*='adgang']", "input[placeholder*='kode']"
    ]
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
            btn = driver.find_element(By.XPATH,
                "//button[contains(translate(.,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'log ind') or contains(translate(.,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'login')]")
        except Exception:
            pass
    if not btn:
        print("❌ Kunne ikke finde login-knap")
        write_debug(driver, "debug_login")
        driver.quit()
        sys.exit(1)

    btn.click()
    try:
        wait.until(EC.url_contains("dashboard"))
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
    wait = WebDriverWait(driver, 15)
    driver.get(f"{MINUBA_URL}/#/mytimeregistration")
    time.sleep(2)
    click_by_text(driver, ["Min tid", "MinTid", "Min Time", "My time"])
    time.sleep(2)
    if "mytimeregistration" not in driver.current_url and "mintime" not in driver.current_url:
        try:
            links = driver.find_elements(By.XPATH,
                "//*[contains(translate(text(),'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'min tid') or contains(@href,'mytimeregistration') or contains(@href,'mintime')]")
            if links:
                links[0].click()
                time.sleep(2)
        except Exception:
            pass
    try:
        wait.until(EC.presence_of_element_located((By.CSS_SELECTOR,
            "input[class*='cal'], input[class*='hasDatepicker'], input.z-textbox.hasDatepicker, input[type='date']")))
    except Exception:
        pass
    print(f"   URL: {driver.current_url}")


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


def click_next(driver) -> bool:
    selectors = [
        "div.arrowWrap[title*='Næste']", "div.arrowWrap[title*='Next']", "div.arrow-right",
        "button[title*='Næste']", "button[aria-label*='Næste']",
        "button[title*='Next']", "button[aria-label*='Next']",
        "a[title*='Næste']", "a[aria-label*='Næste']", "a[title*='Next']",
        "button.z-calendar-next", "a.z-calendar-next", "span.z-cal-arrow-next",
        "button.z-calendar-btn-right", "a.z-calendar-btn-right",
    ]
    for css in selectors:
        try:
            for btn in driver.find_elements(By.CSS_SELECTOR, css):
                if btn.is_displayed() and btn.is_enabled():
                    driver.execute_script("arguments[0].scrollIntoView({block:'center'}); arguments[0].click();", btn)
                    return True
        except Exception:
            continue
    try:
        return bool(driver.execute_script("""
            var pats=[/n[æa]ste/i,/next/i];
            var els=document.querySelectorAll('a,button,span,div');
            for(var i=0;i<els.length;i++){
              var t=(els[i].innerText||els[i].textContent||'').trim();
              var title=(els[i].title||'').trim();
              if((pats.some(p=>p.test(t)) || pats.some(p=>p.test(title))) && els[i].offsetParent!==null){els[i].click(); return true;}
            }
            return false;
        """))
    except Exception:
        return False


def click_prev(driver) -> bool:
    selectors = [
        "div.arrowWrap[title*='Forrige']", "div.arrowWrap[title*='Prev']", "div.arrow-left",
        "button[title*='Forrige']", "button[aria-label*='Forrige']",
        "button[title*='Prev']", "button[aria-label*='Prev']",
        "a[title*='Forrige']", "a[aria-label*='Forrige']",
        "button.z-calendar-prev", "a.z-calendar-prev", "span.z-cal-arrow-prev",
        "button.z-calendar-btn-left", "a.z-calendar-btn-left",
    ]
    for css in selectors:
        try:
            for btn in driver.find_elements(By.CSS_SELECTOR, css):
                if btn.is_displayed() and btn.is_enabled():
                    driver.execute_script("arguments[0].scrollIntoView({block:'center'}); arguments[0].click();", btn)
                    return True
        except Exception:
            continue
    try:
        return bool(driver.execute_script("""
            var pats=[/forrige/i,/prev/i,/tilbage/i,/back/i];
            var els=document.querySelectorAll('a,button,span,div');
            for(var i=0;i<els.length;i++){
              var t=(els[i].innerText||els[i].textContent||'').trim();
              var title=(els[i].title||'').trim();
              if((pats.some(p=>p.test(t)) || pats.some(p=>p.test(title))) && els[i].offsetParent!==null){els[i].click(); return true;}
            }
            return false;
        """))
    except Exception:
        return False


def navigate_to_start_date(driver, fra: date):
    frastr = fra.strftime("%d-%m-%Y")
    print(f"📍 Sætter startdato i feltet: {frastr}")

    for attempt in range(3):
        el = find_visible(driver, DATE_INPUT_SELECTORS)
        if not el:
            time.sleep(1)
            continue

        current_val = (el.get_attribute("value") or "").strip()
        print(f"   Datofelt fundet (forsøg {attempt + 1}), nuværende værdi: {current_val!r}")

        try:
            driver.execute_script("arguments[0].scrollIntoView({block:'center'}); arguments[0].click();", el)
            time.sleep(0.3)

            driver.execute_script(
                "var el=arguments[0], val=arguments[1];"
                "el.focus();"
                "el.value=val;"
                "el.setAttribute('value', val);"
                "el.dispatchEvent(new Event('input',{bubbles:true}));"
                "el.dispatchEvent(new Event('change',{bubbles:true}));"
                "el.dispatchEvent(new Event('blur',{bubbles:true}));",
                el, frastr
            )
            time.sleep(0.4)

            try:
                el.send_keys(Keys.CONTROL, 'a')
                el.send_keys(frastR if False else frastr)
                el.send_keys(Keys.RETURN)
            except Exception:
                pass

            time.sleep(1.5)
            new_val = (el.get_attribute("value") or "").strip()
            if frastr in new_val or new_val == frastr:
                print(f"   ✅ Datofelt sat til {new_val}")
                return True

            try:
                body = driver.find_element(By.TAG_NAME, 'body').text
                if frastr in body:
                    print("   ✅ Siden viser nu startdatoen")
                    return True
            except Exception:
                pass

            print(f"   ⚠️ Datofelt blev ikke bekræftet, læst værdi: {new_val!r}")
        except Exception as e:
            print(f"   ⚠️ Forsøg {attempt + 1} fejlede: {e}")
        time.sleep(1)

    print("   ⚠️ Kunne ikke sætte dato direkte i feltet — fallback til kalendernavigation")
    current = get_cal_date(driver)
    if current:
        days_back = (current - fra).days
        if 0 < days_back < 400:
            print(f"   ↩️ Klikker Forrige ca. {days_back} gange som fallback")
            for i in range(days_back):
                if not click_prev(driver):
                    print(f"   ⚠️ Forrige-klik stoppede efter {i} klik")
                    break
                time.sleep(0.1 if i % 10 != 0 else 0.4)
            return True

    print("   ⚠️ Startdato kunne ikke sættes")
    return False


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
                print(f"   ⚠️ {d}: task-sum={task_sum:.2f}t ≠ reg_timer={reg_timer:.2f}t")
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
    time.sleep(3)
    try:
        body_text = driver.find_element(By.TAG_NAME, "body").text.lower()
        if "min plan" in body_text and "min tid" in body_text:
            click_by_text(driver, ["Min tid", "MinTid", "Min Time", "My time"])
            time.sleep(2)
    except Exception:
        pass

    print(f"📍 Navigerer til startdato: {fra}")
    navigate_to_start_date(driver, fra)
    time.sleep(2)

    all_entries = []
    seen_keys = set()
    current_date = get_cal_date(driver) or fra
    max_steps = (til - fra).days + 5
    step = 0
    last_body = None
    no_change_count = 0

    print(f"⏳ Gennemgår dage fra {fra} til {til} ({(til - fra).days + 1} dage)")
    while step < max_steps:
        try:
            body_text = driver.find_element(By.TAG_NAME, "body").text
        except Exception:
            body_text = ""

        if body_text == last_body:
            no_change_count += 1
            if no_change_count >= 3:
                print("   ⚠️ Siden ændrer sig ikke — stopper navigation")
                break
        else:
            no_change_count = 0
            last_body = body_text

        progress_pct = min(100, int(((step + 1) / max_steps) * 100))
        print(f"   → Dag {step + 1}/{max_steps} | dato: {current_date} | fundet indtil nu: {len(all_entries)} poster | {progress_pct}%")
        year_hint = current_date.year
        page_entries = parse_page_body(body_text, year_hint)
        for e in page_entries:
            d = e.get("date")
            h = round(float(e.get("hours", 0) or 0), 2)
            t = str(e.get("type", "")).strip().lower()[:40]
            key = (d, h, t)
            if key not in seen_keys:
                seen_keys.add(key)
                all_entries.append(e)
                print(f"      ✅ Ny post: {d} | {h:.2f} t | {classify(e.get('type',''))} | {str(e.get('type',''))[:50]}")

        if current_date >= til:
            break
        print(f"   ↪ Går videre til næste dag fra {current_date}...")
        clicked = click_next(driver)
        if not clicked:
            print("   ⚠️ Kunne ikke klikke Næste — stopper")
            break
        time.sleep(0.8)
        new_date = get_cal_date(driver)
        if new_date and new_date != current_date:
            current_date = new_date
        else:
            current_date = current_date + timedelta(days=1)
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
        if d is not None and not (fra <= d <= til):
            continue
        if key in seen_keys:
            continue
        seen_keys.add(key)
        filtered.append(e)
    return filtered


def entries_to_dataframe(entries):
    rows = []
    for e in entries:
        d = e.get("date") if isinstance(e.get("date"), date) else parse_date_any(str(e.get("date") or ""))
        typ = str(e.get("type") or "Ukendt")
        kat = classify(typ)
        hours = round(float(e.get("hours", 0) or 0), 2)
        rows.append({
            "dato": d.isoformat() if d else "",
            "kategori": kat,
            "type": typ,
            "timer": hours,
            "rå": str(e.get("raw") or "")
        })
    return pd.DataFrame(rows)


def build_summary(entries):
    totals = defaultdict(float)
    monthly = defaultdict(lambda: defaultdict(float))
    for e in entries:
        hours = float(e.get("hours", 0) or 0)
        cat = classify(str(e.get("type", "")))
        d = e.get("date") if isinstance(e.get("date"), date) else parse_date_any(str(e.get("date") or ""))
        month_key = d.strftime("%Y-%m") if d else "ukendt"
        totals[cat] += hours
        monthly[month_key][cat] += hours
    arbejde = round(totals.get("Arbejde", 0.0), 2)
    ferie = round(totals.get("Ferie", 0.0), 2)
    sygdom = round(totals.get("Sygdom", 0.0), 2)
    total = round(arbejde + ferie + sygdom, 2)
    months = []
    for m in sorted(monthly.keys()):
        d = monthly[m]
        t = round(sum(d.values()), 2)
        months.append({
            "måned": m,
            "arbejde": round(d.get("Arbejde", 0.0), 2),
            "ferie": round(d.get("Ferie", 0.0), 2),
            "sygdom": round(d.get("Sygdom", 0.0), 2),
            "total": t,
        })
    return {"arbejde": arbejde, "ferie": ferie, "sygdom": sygdom, "total": total, "months": months}


def print_rapport(entries: list, fra: date, til: date, vis_typer: bool = False):
    if not entries:
        print("\n⚠️ Ingen tidsregistreringer fundet.")
        print("   Prøv --no-headless og tjek at kalenderen viser startdatoen korrekt.")
        return
    summary = build_summary(entries)
    print("\n" + "═" * 62)
    print("  MINUBA TIMER OVERSIGT")
    print(f"  Periode: {fra} → {til}")
    print("═" * 62)
    print(f"  {'Arbejdstimer':<24} {summary['arbejde']:>8.2f} t")
    print(f"  {'Ferie':<24} {summary['ferie']:>8.2f} t")
    print(f"  {'Sygdom':<24} {summary['sygdom']:>8.2f} t")
    print("  " + "─" * 34)
    print(f"  {'TOTAL':<24} {summary['total']:>8.2f} t")
    print("═" * 62)
    if len(summary['months']) > 1:
        print("\n  MÅNEDSOPDELING:")
        print("  " + "─" * 58)
        print(f"  {'Måned':<10} {'Arbejde':>10} {'Ferie':>10} {'Sygdom':>10} {'Total':>10}")
        print("  " + "─" * 58)
        for m in summary['months']:
            print(f"  {m['måned']:<10} {m['arbejde']:>10.2f} {m['ferie']:>10.2f} {m['sygdom']:>10.2f} {m['total']:>10.2f}")
    if vis_typer:
        type_totals = defaultdict(float)
        for e in entries:
            t3 = str(e.get("type") or "Ukendt")
            type_totals[t3] += float(e.get("hours", 0) or 0)
        print("\n  ALLE TYPER FUNDET:")
        print("  " + "─" * 60)
        for t3, h in sorted(type_totals.items(), key=lambda x: -x[1]):
            print(f"  [{classify(t3):8s}] {t3:<40} {h:>7.2f} t")


def render_html_report(df, summary, fra, til, navn=None, virksomhed=None, timeløn=None, kommentar=None):
    generated = datetime.now().strftime("%Y-%m-%d %H:%M")
    rows_html = "".join(
        f"<tr><td>{r['dato']}</td><td>{r['kategori']}</td><td>{r['type']}</td><td class='num'>{float(r['timer']):.2f}</td></tr>"
        for _, r in df.sort_values(["dato", "kategori", "type"]).iterrows()
    ) if not df.empty else "<tr><td colspan='4'>Ingen registreringer fundet.</td></tr>"
    month_html = "".join(
        f"<tr><td>{m['måned']}</td><td class='num'>{m['arbejde']:.2f}</td><td class='num'>{m['ferie']:.2f}</td><td class='num'>{m['sygdom']:.2f}</td><td class='num'>{m['total']:.2f}</td></tr>"
        for m in summary['months']
    ) if summary['months'] else "<tr><td colspan='5'>Ingen månedsopdeling tilgængelig.</td></tr>"

    løn_html = ""
    if timeløn is not None:
        forventet = round(summary['arbejde'] * timeløn, 2)
        løn_html = f"""
        <div class='card'>
          <h2>Foreløbig lønberegning</h2>
          <p>Ved en oplyst timeløn på <strong>{timeløn:.2f} kr.</strong> giver arbejdstimerne en forventet grundløn på <strong>{forventet:.2f} kr.</strong>.</p>
          <p class='muted'>Beløbet tager ikke automatisk højde for pension, tillæg, fritvalg, feriepenge, SH/FV, overarbejde eller lokale aftaler.</p>
        </div>
        """

    kommentar_html = f"<div class='card'><h2>Kommentar</h2><p>{kommentar}</p></div>" if kommentar else ""

    return f"""
<!doctype html>
<html lang='da'>
<head>
<meta charset='utf-8'>
<title>Lønkontrol rapport</title>
<style>
  @page {{ size: A4; margin: 16mm; }}
  body {{ font-family: Arial, sans-serif; color:#1f2937; font-size:12px; line-height:1.45; }}
  h1,h2,h3 {{ margin:0 0 8px 0; color:#0f172a; }}
  h1 {{ font-size:24px; }}
  h2 {{ font-size:16px; margin-top:18px; }}
  .top {{ display:flex; justify-content:space-between; align-items:flex-start; margin-bottom:16px; }}
  .brand {{ font-weight:700; color:#0f766e; font-size:14px; }}
  .muted {{ color:#6b7280; }}
  .grid {{ display:grid; grid-template-columns: repeat(2, 1fr); gap:10px; margin:14px 0; }}
  .grid4 {{ display:grid; grid-template-columns: repeat(4, 1fr); gap:10px; margin:14px 0; }}
  .card {{ border:1px solid #d1d5db; border-radius:8px; padding:10px 12px; background:#f8fafc; }}
  .kpi {{ font-size:22px; font-weight:700; margin-top:6px; }}
  table {{ width:100%; border-collapse:collapse; margin-top:8px; }}
  th, td {{ border:1px solid #d1d5db; padding:7px 8px; vertical-align:top; }}
  th {{ background:#e2e8f0; text-align:left; }}
  .num {{ text-align:right; white-space:nowrap; }}
  .small {{ font-size:10px; }}
  .note {{ background:#fff7ed; border:1px solid #fdba74; border-radius:8px; padding:10px 12px; margin-top:14px; }}
  .footer {{ margin-top:18px; font-size:10px; color:#6b7280; }}
</style>
</head>
<body>
  <div class='top'>
    <div>
      <div class='brand'>Lønkontrol rapport</div>
      <h1>Timer fra Minuba</h1>
      <div class='muted'>Periode: {fra} til {til}</div>
    </div>
    <div class='small muted'>Genereret: {generated}</div>
  </div>

  <div class='grid'>
    <div class='card'><strong>Navn</strong><br>{navn or 'Ikke angivet'}</div>
    <div class='card'><strong>Virksomhed</strong><br>{virksomhed or 'Ikke angivet'}</div>
  </div>

  <div class='grid4'>
    <div class='card'><div>Arbejdstimer</div><div class='kpi'>{summary['arbejde']:.2f} t</div></div>
    <div class='card'><div>Ferie</div><div class='kpi'>{summary['ferie']:.2f} t</div></div>
    <div class='card'><div>Sygdom</div><div class='kpi'>{summary['sygdom']:.2f} t</div></div>
    <div class='card'><div>Total</div><div class='kpi'>{summary['total']:.2f} t</div></div>
  </div>

  {løn_html}
  {kommentar_html}

  <div class='card'>
    <h2>Formål</h2>
    <p>Denne rapport er lavet som dokumentation til gennemgang af registrerede timer og kan bruges som bilag ved forespørgsel om lønkontrol, herunder ved henvendelse til Dansk El-Forbund.</p>
    <p>Scriptet er udviklet uafhængigt og er ikke officielt tilknyttet Minuba eller Dansk El-Forbund. Det er baseret på data hentet direkte fra brugerens Minuba-konto og kategoriserer timerne ud fra de typer og beskrivelser, der findes i registreringerne.</p>
    <p>Scriptet er udviklet af Benjamin Kallehave</p>
  </div>

  <h2>Månedsopdeling</h2>
  <table>
    <thead>
      <tr><th>Måned</th><th class='num'>Arbejde</th><th class='num'>Ferie</th><th class='num'>Sygdom</th><th class='num'>Total</th></tr>
    </thead>
    <tbody>{month_html}</tbody>
  </table>

  <h2>Detaljerede registreringer</h2>
  <table>
    <thead>
      <tr><th>Dato</th><th>Kategori</th><th>Type</th><th class='num'>Timer</th></tr>
    </thead>
    <tbody>{rows_html}</tbody>
  </table>

  <div class='note'>
    <strong>Bemærkning:</strong> Kategorisering af ferie og sygdom er lavet ud fra navne/typer fundet i registreringerne. Hvis virksomheden bruger andre betegnelser, bør rapporten gennemgås manuelt før den sendes videre.
  </div>

  <div class='footer'>
    Rapporten er genereret automatisk på baggrund af registreringer hentet fra Minuba via brugerens login. Dokumentet er tænkt som støtte til lønkontrol og bør sammenholdes med lønsedler, timeløn, overenskomst og eventuelle tillæg.
  </div>
</body>
</html>
"""


def save_pdf_report(html, output_pdf):
    if HTML is None:
        print("❌ WeasyPrint er ikke installeret. Installer fx: sudo pacman -S python-weasyprint")
        sys.exit(1)
    HTML(string=html).write_pdf(output_pdf)


def main():
    parser = argparse.ArgumentParser(
        description="Henter timer fra Minuba (Min Tid) og kan lave PDF-rapport.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
EKSEMPLER:
python minuba_timer.py --email din@email.dk --adgangskode DinKode --periode 2025-04
python minuba_timer.py --email din@email.dk --adgangskode DinKode --periode 2025-04 --pdf april.pdf
python minuba_timer.py --email din@email.dk --adgangskode DinKode --fra 2025-01-01 --til 2025-03-31 --pdf q1.pdf --navn 'Dit Navn' --virksomhed 'Firma ApS' --timeløn 220
"""
    )
    parser.add_argument("--email", required=False)
    parser.add_argument("--adgangskode", required=False)
    parser.add_argument("--periode", default=None)
    parser.add_argument("--fra", default=None)
    parser.add_argument("--til", default=None)
    parser.add_argument("--no-headless", action="store_true")
    parser.add_argument("--vis-typer", action="store_true")
    parser.add_argument("--pdf", default=None, help="Gem rapport som PDF, fx rapport.pdf")
    parser.add_argument("--csv", default=None, help="Gem detaljer som CSV")
    parser.add_argument("--navn", default=None)
    parser.add_argument("--virksomhed", default=None)
    parser.add_argument("--timeløn", type=float, default=None)
    parser.add_argument("--kommentar", default=None)
    args = parser.parse_args()

    if not args.email:
        args.email = input("Email: ").strip()
    if not args.adgangskode:
        args.adgangskode = getpass.getpass("Adgangskode: ").strip()
    if not args.email or not args.adgangskode:
        print("❌ Email og adgangskode er påkrævet")
        sys.exit(1)

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
            print(f"   → {len(entries)} poster i perioden.")
        print_rapport(entries, fra, til, vis_typer=args.vis_typer)
    finally:
        driver.quit()

    df = entries_to_dataframe(entries)
    summary = build_summary(entries)

    if args.csv:
        df.to_csv(args.csv, index=False)
        print(f"💾 CSV gemt: {args.csv}")

    if args.pdf:
        html = render_html_report(df, summary, fra, til, navn=args.navn, virksomhed=args.virksomhed, timeløn=args.timeløn, kommentar=args.kommentar)
        save_pdf_report(html, args.pdf)
        print(f"🧾 PDF gemt: {args.pdf}")


if __name__ == "__main__":
    main()
