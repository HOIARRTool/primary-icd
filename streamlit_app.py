import json
import os
from datetime import datetime, date, time
from zoneinfo import ZoneInfo

import gspread
import pandas as pd
import streamlit as st


# =========================
# Config
# =========================
APP_TITLE = "ระบบบันทึกอุบัติการณ์ความคลาดเคลื่อนทางยา (Medication Error)"
THAI_TZ = ZoneInfo("Asia/Bangkok")

PROCESS_OPTIONS = ["สั่งใช้ยา", "จัด/จ่ายยา", "ให้ยา", "ผู้ป่วยใช้ยาผิดวิธี"]
SEVERITY_OPTIONS = ["A", "B", "C", "D", "E", "F", "G", "H", "I"]

HEADERS = [
    "เวลาบันทึกระบบ",
    "วันที่เกิดเหตุ",
    "เวลาเกิดเหตุ",
    "กระบวนการที่เกิด",
    "ชื่อยา",
    "ระดับความรุนแรง",
    "รายละเอียดเหตุการณ์",
]


# =========================
# Utility: Read secrets/env
# =========================
def _safe_get_st_secret(key, default=None):
    """Safely get st.secrets[key] without crashing when secrets file doesn't exist."""
    try:
        return st.secrets[key]
    except Exception:
        return default


def _normalize_private_key(creds: dict) -> dict:
    if "private_key" in creds and isinstance(creds["private_key"], str):
        # รองรับกรณีเก็บใน env แล้ว \n ถูก escape มา
        creds["private_key"] = creds["private_key"].replace("\\n", "\n")
    return creds


def load_google_config():
    """
    Priority:
    1) st.secrets["gcp_service_account"] + st.secrets["gsheets"]
    2) ENV: GCP_SERVICE_ACCOUNT_JSON + GSHEET_URL + GSHEET_WORKSHEET
    3) ENV: GCP_SERVICE_ACCOUNT_FILE + GSHEET_URL + GSHEET_WORKSHEET
    """
    # --- 1) Streamlit secrets ---
    svc = _safe_get_st_secret("gcp_service_account", None)
    gsheet_cfg = _safe_get_st_secret("gsheets", None)

    if svc and gsheet_cfg:
        creds_dict = dict(svc)
        creds_dict = _normalize_private_key(creds_dict)

        spreadsheet_url = gsheet_cfg.get("spreadsheet_url", "").strip()
        worksheet_name = gsheet_cfg.get("worksheet", "MedicationError").strip() or "MedicationError"

        if not spreadsheet_url:
            raise ValueError("ไม่พบ gsheets.spreadsheet_url ใน secrets.toml")

        return creds_dict, spreadsheet_url, worksheet_name

    # --- 2) ENV JSON ---
    env_json = os.getenv("GCP_SERVICE_ACCOUNT_JSON", "").strip()
    spreadsheet_url = os.getenv("GSHEET_URL", "").strip()
    worksheet_name = os.getenv("GSHEET_WORKSHEET", "MedicationError").strip() or "MedicationError"

    if env_json and spreadsheet_url:
        try:
            creds_dict = json.loads(env_json)
            creds_dict = _normalize_private_key(creds_dict)
        except json.JSONDecodeError as e:
            raise ValueError(f"GCP_SERVICE_ACCOUNT_JSON ไม่ใช่ JSON ที่ถูกต้อง: {e}") from e

        return creds_dict, spreadsheet_url, worksheet_name

    # --- 3) ENV secret file path ---
    creds_file = os.getenv("GCP_SERVICE_ACCOUNT_FILE", "").strip()
    if creds_file and spreadsheet_url:
        if not os.path.exists(creds_file):
            raise FileNotFoundError(f"ไม่พบไฟล์ credentials ตาม path: {creds_file}")

        with open(creds_file, "r", encoding="utf-8") as f:
            creds_dict = json.load(f)

        creds_dict = _normalize_private_key(creds_dict)
        return creds_dict, spreadsheet_url, worksheet_name

    raise RuntimeError(
        "ยังไม่ได้ตั้งค่า Google credentials / Google Sheet\n"
        "- ใช้ .streamlit/secrets.toml (local) หรือ\n"
        "- ตั้ง ENV: GCP_SERVICE_ACCOUNT_JSON + GSHEET_URL (+ GSHEET_WORKSHEET) หรือ\n"
        "- ตั้ง ENV: GCP_SERVICE_ACCOUNT_FILE + GSHEET_URL (+ GSHEET_WORKSHEET)"
    )


# =========================
# Google Sheets Connection
# =========================
@st.cache_resource
def get_worksheet():
    creds_dict, spreadsheet_url, worksheet_name = load_google_config()

    gc = gspread.service_account_from_dict(creds_dict)
    sh = gc.open_by_url(spreadsheet_url)

    try:
        ws = sh.worksheet(worksheet_name)
    except gspread.exceptions.WorksheetNotFound:
        # ถ้ายังไม่มีชีตนี้ ให้สร้างใหม่
        ws = sh.add_worksheet(title=worksheet_name, rows=1000, cols=max(20, len(HEADERS) + 5))

    ensure_headers(ws)
    return ws


def ensure_headers(ws):
    """Ensure row 1 contains the expected headers."""
    current_headers = ws.row_values(1)
    if current_headers[: len(HEADERS)] != HEADERS:
        ws.update("A1:G1", [HEADERS])


def append_incident(ws, event_date, event_time, process, drug_name, severity, details):
    now_str = datetime.now(THAI_TZ).strftime("%Y-%m-%d %H:%M:%S")
    event_date_str = event_date.strftime("%Y-%m-%d")
    event_time_str = event_time.strftime("%H:%M")

    row = [
        now_str,
        event_date_str,
        event_time_str,
        process,
        drug_name.strip(),
        severity,
        details.strip(),
    ]

    ws.append_row(row, value_input_option="USER_ENTERED")


@st.cache_data(ttl=15)
def load_records():
    ws = get_worksheet()
    records = ws.get_all_records(default_blank="")

    if not records:
        return pd.DataFrame(columns=HEADERS)

    df = pd.DataFrame(records)

    # เผื่อหัวคอลัมน์ในชีตไม่ครบ/ลำดับผิด
    for col in HEADERS:
        if col not in df.columns:
            df[col] = ""

    df = df[HEADERS].copy()

    # สร้างคอลัมน์ช่วย sort ตามวันที่+เวลาเหตุการณ์
    dt_text = df["วันที่เกิดเหตุ"].astype(str).str.strip() + " " + df["เวลาเกิดเหตุ"].astype(str).str.strip()
    df["_sort_dt"] = pd.to_datetime(dt_text, errors="coerce")

    df = df.sort_values(by="_sort_dt", ascending=False, na_position="last").drop(columns=["_sort_dt"])
    return df


# =========================
# UI
# =========================
def render_form_tab():
    st.subheader("📝 บันทึกข้อมูลอุบัติการณ์")

    col1, col2 = st.columns(2)
    with col1:
        event_date = st.date_input("วันที่เกิดเหตุ", value=date.today())
    with col2:
        event_time = st.time_input("เวลาเกิดเหตุ", value=datetime.now(THAI_TZ).time().replace(second=0, microsecond=0))

    process = st.selectbox("กระบวนการที่เกิด", PROCESS_OPTIONS)
    drug_name = st.text_input("ชื่อยา", placeholder="เช่น Warfarin / Insulin / Ceftriaxone")
    severity = st.selectbox("ระดับความรุนแรง", SEVERITY_OPTIONS)
    details = st.text_area("รายละเอียดเหตุการณ์", height=180, placeholder="กรอกรายละเอียดเหตุการณ์ที่เกิดขึ้น...")

    if st.button("บันทึกข้อมูล", type="primary", use_container_width=True):
        # Validation พื้นฐาน
        errors = []
        if not drug_name.strip():
            errors.append("กรุณากรอกชื่อยา")
        if not details.strip():
            errors.append("กรุณากรอกรายละเอียดเหตุการณ์")

        if errors:
            for e in errors:
                st.error(e)
            return

        try:
            ws = get_worksheet()
            append_incident(
                ws=ws,
                event_date=event_date,
                event_time=event_time,
                process=process,
                drug_name=drug_name,
                severity=severity,
                details=details,
            )
            load_records.clear()  # clear cache after write
            st.success("บันทึกข้อมูลสำเร็จ")
        except Exception as e:
            st.error(f"บันทึกข้อมูลไม่สำเร็จ: {e}")


def render_history_tab():
    st.subheader("📚 ดูข้อมูลย้อนหลัง")

    try:
        df = load_records()
    except Exception as e:
        st.error(f"อ่านข้อมูลจาก Google Sheets ไม่สำเร็จ: {e}")
        return

    if df.empty:
        st.info("ยังไม่มีข้อมูลในชีตนี้")
        return

    # Filters
    st.markdown("### ตัวกรอง")
    c1, c2, c3, c4 = st.columns([1, 1, 1, 2])

    # แปลงวันที่สำหรับกรอง
    df_filter = df.copy()
    df_filter["_event_date"] = pd.to_datetime(df_filter["วันที่เกิดเหตุ"], errors="coerce").dt.date

    min_d = df_filter["_event_date"].dropna().min() or date.today()
    max_d = df_filter["_event_date"].dropna().max() or date.today()

    with c1:
        start_date = st.date_input("วันที่เริ่ม", value=min_d, key="hist_start")
    with c2:
        end_date = st.date_input("วันที่สิ้นสุด", value=max_d, key="hist_end")
    with c3:
        severity_filter = st.multiselect("ระดับความรุนแรง", SEVERITY_OPTIONS, default=[])
    with c4:
        keyword = st.text_input("ค้นหา (ชื่อยา/รายละเอียด)", placeholder="พิมพ์คำค้น...", key="hist_keyword")

    # ตัวกรองเพิ่มเติม
    process_filter = st.multiselect("กระบวนการที่เกิด", PROCESS_OPTIONS, default=[])

    filtered = df_filter.copy()

    filtered = filtered[
        (filtered["_event_date"].isna()) |
        ((filtered["_event_date"] >= start_date) & (filtered["_event_date"] <= end_date))
    ]

    if severity_filter:
        filtered = filtered[filtered["ระดับความรุนแรง"].isin(severity_filter)]

    if process_filter:
        filtered = filtered[filtered["กระบวนการที่เกิด"].isin(process_filter)]

    if keyword.strip():
        kw = keyword.strip().lower()
        filtered = filtered[
            filtered["ชื่อยา"].astype(str).str.lower().str.contains(kw, na=False)
            | filtered["รายละเอียดเหตุการณ์"].astype(str).str.lower().str.contains(kw, na=False)
        ]

    filtered = filtered.drop(columns=["_event_date"])

    # Metrics
    m1, m2, m3 = st.columns(3)
    m1.metric("จำนวนรายการทั้งหมด", f"{len(df):,}")
    m2.metric("ผลลัพธ์ตามตัวกรอง", f"{len(filtered):,}")
    m3.metric("ระดับ E-I", f"{filtered['ระดับความรุนแรง'].isin(['E','F','G','H','I']).sum():,}")

    st.dataframe(filtered, use_container_width=True, hide_index=True)


def main():
    st.set_page_config(page_title="Medication Error Logger", page_icon="💊", layout="wide")

    st.title("💊 " + APP_TITLE)
    st.caption("บันทึกจากหน้าเว็บ → เก็บใน Google Sheets (Hybrid)")

    # ตรวจ config เบื้องต้น
    with st.expander("🔧 สถานะการเชื่อมต่อ", expanded=False):
        try:
            _, sheet_url, worksheet_name = load_google_config()
            st.success("ตั้งค่า credentials / sheet ครบแล้ว")
            st.write(f"Worksheet: `{worksheet_name}`")
            st.write(f"Sheet URL: {sheet_url}")
        except Exception as e:
            st.warning("ยังตั้งค่าไม่ครบ")
            st.code(str(e))

    tab1, tab2 = st.tabs(["บันทึกข้อมูล", "ดูข้อมูลย้อนหลัง"])
    with tab1:
        render_form_tab()
    with tab2:
        render_history_tab()


if __name__ == "__main__":
    main()
