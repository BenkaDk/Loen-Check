#!/usr/bin/env python3
"""
Løncheck GUI — app.py
Kræver: pip install streamlit selenium webdriver-manager pandas weasyprint openpyxl reportlab pdfplumber
Start:  streamlit run app.py
"""

import io
import queue
import sys
import tempfile
import threading
from pathlib import Path

import streamlit as st

from lonseddel_analyse import analyze_payslip, write_csv as write_payslip_csv
from reconcile import load_minuba_csv, load_payslip_csv, reconcile, make_pdf

st.set_page_config(page_title="Løncheck", page_icon="💰", layout="wide")
st.title("💰 Løncheck")
st.caption("Dette værktøj er kun en indikation og ikke en erstatning for officiel lønkontrol. Der kan forekomme fejl i data og fortolkning, og systemet opdateres løbende for at blive bedre.")
st.caption("Udviklet af Benjamin Kallehave")
tab1, tab2, tab3 = st.tabs(["1️⃣ Hent Minuba-timer", "2️⃣ Lønsedler", "3️⃣ Sammenlign"])


# ─────────────────────────────────────────────────────────────────
# TAB 1 — Scrape Minuba
# ─────────────────────────────────────────────────────────────────
with tab1:
    st.header("Hent timer fra Minuba")
    email = st.text_input("Email", key="email")
    adgangskode = st.text_input("Adgangskode", type="password", key="adgangskode")
    periode = st.text_input(
        "Periode",
        value="2025",
        help="Eks: 2025 (hele året), 2025-04 (april), 2025-Q1 (kvartal), eller brug Fra/Til",
        key="periode",
    )
    col_fra, col_til = st.columns(2)
    with col_fra:
        custom_fra = st.text_input("Fra (valgfri, YYYY-MM-DD)", key="fra")
    with col_til:
        custom_til = st.text_input("Til (valgfri, YYYY-MM-DD)", key="til")

    if st.button("🔍 Hent timer fra Minuba"):
        if not email or not adgangskode:
            st.error("Email og adgangskode er påkrævet")
        else:
            from minuba_timer import (
                build_driver,
                build_summary,
                entries_to_dataframe,
                filter_entries_by_date,
                get_date_range,
                login,
                navigate_to_min_tid,
                scrape_time_entries,
            )

            fra, til = get_date_range(
                periode if not (custom_fra and custom_til) else None,
                custom_fra or None,
                custom_til or None,
            )

            log_queue: queue.Queue = queue.Queue()
            result_holder: dict = {}

            class QueueWriter(io.TextIOBase):
                def write(self, msg):
                    if msg.strip():
                        log_queue.put(msg.strip())
                    return len(msg)

            def run_scraper():
                old_stdout = sys.stdout
                sys.stdout = QueueWriter()
                try:
                    driver = build_driver(headless=True)
                    try:
                        login(driver, email, adgangskode)
                        navigate_to_min_tid(driver)
                        entries = scrape_time_entries(driver, fra, til)
                        entries = filter_entries_by_date(entries, fra, til)
                        result_holder["entries"] = entries
                    finally:
                        driver.quit()
                except Exception as e:
                    log_queue.put(f"❌ FEJL: {e}")
                    result_holder["error"] = str(e)
                finally:
                    sys.stdout = old_stdout
                    log_queue.put("__DONE__")

            thread = threading.Thread(target=run_scraper, daemon=True)
            thread.start()

            st.info(f"📅 Scraper periode: {fra} → {til}")
            log_box = st.empty()
            log_lines: list = []

            while True:
                try:
                    line = log_queue.get(timeout=0.3)
                    if line == "__DONE__":
                        break
                    log_lines.append(line)
                    log_box.code("\n".join(log_lines[-25:]), language=None)
                except queue.Empty:
                    continue

            if "error" in result_holder:
                st.error(f"Scraping fejlede: {result_holder['error']}")
            else:
                entries = result_holder["entries"]
                df = entries_to_dataframe(entries)
                summary = build_summary(entries)

                with tempfile.NamedTemporaryFile(delete=False, suffix=".csv") as f:
                    df.to_csv(f.name, index=False)
                    st.session_state["minuba_csv_path"] = f.name

                st.success(f"✅ {len(entries)} poster hentet!")
                c1, c2, c3, c4 = st.columns(4)
                c1.metric("Arbejdstimer", f"{summary['arbejde']:.2f} t")
                c2.metric("Sygetimer", f"{summary['sygdom']:.2f} t")
                c3.metric("Ferietimer", f"{summary['ferie']:.2f} t")
                c4.metric("Total", f"{summary['total']:.2f} t")
                st.dataframe(df, use_container_width=True)

                st.download_button(
                    "💾 Download Minuba CSV",
                    data=Path(st.session_state["minuba_csv_path"]).read_bytes(),
                    file_name="minuba_timer.csv",
                    mime="text/csv",
                )

    st.divider()
    st.caption("Eller upload en eksisterende Minuba CSV direkte:")
    uploaded_minuba_csv = st.file_uploader(
        "Upload Minuba CSV",
        type="csv",
        key="minuba_csv_upload",
    )
    if uploaded_minuba_csv:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".csv") as f:
            f.write(uploaded_minuba_csv.read())
            st.session_state["minuba_csv_path"] = f.name

        try:
            minuba_data = load_minuba_csv(Path(st.session_state["minuba_csv_path"]))
            st.success("✅ Minuba CSV uploadet!")
            c1, c2, c3, c4 = st.columns(4)
            c1.metric("Arbejdstimer", f"{minuba_data['work_hours']:.2f} t")
            c2.metric("Sygetimer", f"{minuba_data['sick_hours']:.2f} t")
            c3.metric("Ferietimer", f"{minuba_data['vacation_hours']:.2f} t")
            c4.metric(
                "Total (arbejde + syg)",
                f"{minuba_data['work_hours'] + minuba_data['sick_hours']:.2f} t",
            )
            st.download_button(
                "💾 Download Minuba CSV",
                data=Path(st.session_state["minuba_csv_path"]).read_bytes(),
                file_name="minuba_timer.csv",
                mime="text/csv",
            )
        except Exception as e:
            st.error(f"Kunne ikke læse Minuba CSV: {e}")


# ─────────────────────────────────────────────────────────────────
# TAB 2 — Lønsedler (upload PDF'er → samlet CSV)
# ─────────────────────────────────────────────────────────────────
with tab2:
    st.header("Upload lønsedler (PDF)")
    st.caption(
        "Upload én eller flere PDF-lønsedler. De bliver analyseret og samlet til én CSV "
        "som bruges i trin 3."
    )

    uploaded_pdfs = st.file_uploader(
        "Vælg PDF-lønsedler",
        type="pdf",
        accept_multiple_files=True,
        key="pdf_upload",
    )

    if uploaded_pdfs:
        rows = []
        errors = []
        for pdf_file in uploaded_pdfs:
            with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
                tmp.write(pdf_file.read())
                tmp_path = Path(tmp.name)
            try:
                row = analyze_payslip(tmp_path)
                row["file"] = pdf_file.name
                rows.append(row)
            except Exception as e:
                errors.append(f"{pdf_file.name}: {e}")

        if errors:
            for err in errors:
                st.warning(f"⚠️ {err}")

        if rows:
            import pandas as pd

            df_payslip = pd.DataFrame(rows)
            st.dataframe(df_payslip, use_container_width=True)

            with tempfile.NamedTemporaryFile(delete=False, suffix=".csv") as f:
                write_payslip_csv(rows, Path(f.name))
                st.session_state["payslip_csv_path"] = f.name

            st.success(f"✅ {len(rows)} lønseddel(er) analyseret!")

            payslip_data = load_payslip_csv(Path(st.session_state["payslip_csv_path"]))
            c1, c2, c3, c4 = st.columns(4)
            c1.metric("Arbejdstimer", f"{payslip_data['work_hours']:.2f} t")
            c2.metric("Bruttoløn", f"{payslip_data['gross_salary']:,.2f} kr.")
            c3.metric("Nettoløn", f"{payslip_data['net_salary']:,.2f} kr.")
            c4.metric("Skat", f"{abs(payslip_data['tax']):,.2f} kr.")

            st.download_button(
                "💾 Download lønseddel CSV",
                data=Path(st.session_state["payslip_csv_path"]).read_bytes(),
                file_name="lonsedler_samlet.csv",
                mime="text/csv",
            )

    st.divider()
    st.caption("Eller upload en allerede samlet lønseddel-CSV direkte:")
    manual_csv = st.file_uploader("Upload lønseddel CSV (valgfri)", type="csv", key="manual_csv")
    if manual_csv:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".csv") as f:
            f.write(manual_csv.read())
            st.session_state["payslip_csv_path"] = f.name
        try:
            payslip_data = load_payslip_csv(Path(st.session_state["payslip_csv_path"]))
            st.success("✅ CSV uploadet!")
            c1, c2, c3 = st.columns(3)
            c1.metric("Arbejdstimer", f"{payslip_data['work_hours']:.2f} t")
            c2.metric("Bruttoløn", f"{payslip_data['gross_salary']:,.2f} kr.")
            c3.metric("Nettoløn", f"{payslip_data['net_salary']:,.2f} kr.")
        except Exception as e:
            st.error(f"Kunne ikke læse lønseddel CSV: {e}")


# ─────────────────────────────────────────────────────────────────
# TAB 3 — Reconcile & PDF-rapport
# ─────────────────────────────────────────────────────────────────
with tab3:
    st.header("Sammenlign Minuba vs. lønseddel")
    st.info(
        "Dette værktøj er tænkt som en vejledende sammenligning, ikke en fast lønkontrol. "
        "Tjek altid dine originale lønsedler og Minuba-data manuelt, og sig endelig til hvis du finder fejl."
    )

    minuba_ready = "minuba_csv_path" in st.session_state
    payslip_ready = "payslip_csv_path" in st.session_state

    c1, c2 = st.columns(2)
    c1.markdown(f"Minuba data: {'✅ Klar' if minuba_ready else '❌ Mangler (udfyld trin 1)'}")
    c2.markdown(f"Lønseddel data: {'✅ Klar' if payslip_ready else '❌ Mangler (udfyld trin 2)'}")

    hourly_rate = st.number_input("Timeløn (kr.)", min_value=0.0, value=220.0, step=1.0)

    if not (minuba_ready and payslip_ready):
        st.warning("Gennemfør trin 1 og 2 først før du kan sammenligne.")
    else:
        if st.button("📊 Analysér nu"):
            minuba = load_minuba_csv(Path(st.session_state["minuba_csv_path"]))
            payslip = load_payslip_csv(Path(st.session_state["payslip_csv_path"]))
            data = reconcile(minuba, payslip, hourly_rate)

            if data["hour_diff"] > 0:
                st.success(data["status"])
            elif data["hour_diff"] < 0:
                st.error(data["status"])
            else:
                st.info(data["status"])

            st.markdown(f"**Konklusion:** {data['conclusion']}")
            st.info("Minuba total er altid arbejde + syge. Ferietimer tæller ikke med i denne sammenligning.")
            if data['payslip_total_hours_ytd'] > 0:
                st.info(f"Lønsedlens år-til-dato timer: {data['payslip_total_hours_ytd']:.2f} t. Total beregnes stadig som arbejde + syge.")
            else:
                st.info("Lønsedlens samlede timer er beregnet som arbejde + syge fra de uploadede poster.")

            c1, c2, c3, c4 = st.columns(4)
            c1.metric("Forskel (timer)", f"{data['hour_diff']:.2f} t")
            c2.metric("Forskel (kr.)", f"{data['money_diff']:,.2f} kr.")
            c3.metric("Bruttoløn (lønseddel)", f"{data['gross_salary']:,.2f} kr.")
            c4.metric("Nettoløn", f"{data['net_salary']:,.2f} kr.")

            st.divider()
            c1, c2 = st.columns(2)
            with c1:
                st.subheader("Minuba")
                st.metric("Arbejdstimer", f"{data['minuba_work_hours']:.2f} t")
                st.metric("Sygetimer", f"{data['minuba_sick_hours']:.2f} t")
                st.metric("Ferietimer", f"{data['minuba_vacation_hours']:.2f} t")
                st.metric("Total betalte timer (arbejde+syg)", f"{data['minuba_total_hours']:.2f} t")
            with c2:
                st.subheader("Lønseddel")
                st.metric("Arbejdstimer", f"{data['payslip_work_hours']:.2f} t")
                st.metric("Sygdage", f"{data.get('payslip_sick_days', 0.0):.2f}")
                st.metric("Total betalte timer (arbejdstimer)", f"{data['payslip_total_hours']:.2f} t")
                if data['payslip_total_hours_ytd'] > 0:
                    st.metric("År-til-dato timer", f"{data['payslip_total_hours_ytd']:.2f} t")

            st.divider()
            st.subheader("Økonomi detaljer")
            c1, c2, c3 = st.columns(3)
            c1.metric("Skat", f"{abs(data['tax']):,.2f} kr.")
            c2.metric("SH i perioden", f"{data['sh_period']:,.2f} kr.")
            c3.metric("SH rest", f"{data['sh_rest']:,.2f} kr.")
            c1.metric("Medarbejderpension", f"{data['pension_employee']:,.2f} kr.")
            c2.metric("Arbejdsgiverpension", f"{data['pension_employer']:,.2f} kr.")
            c3.metric("Pension i alt", f"{data['pension_total']:,.2f} kr.")

            with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as f:
                make_pdf(data, Path(f.name), hourly_rate)
                st.download_button(
                    "📄 Download PDF-rapport",
                    data=Path(f.name).read_bytes(),
                    file_name="loencheck_rapport.pdf",
                    mime="application/pdf",
                    type="primary",
                )