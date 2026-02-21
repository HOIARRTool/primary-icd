import json
import os
import secrets
from datetime import datetime, date
from zoneinfo import ZoneInfo

import gspread
import pandas as pd
import streamlit as st


# =========================
# Constants
# =========================
THAI_TZ = ZoneInfo("Asia/Bangkok")
PROCESS_OPTIONS = ["สั่งใช้ยา", "จัด/จ่ายยา", "ให้ยา", "ผู้ป่วยใช้ยาผิดวิธี"]
SEVERITY_OPTIONS = ["A", "B", "C", "D", "E", "F", "G", "H", "I"]

HEADERS = [
    "เวลาบันทึกระบบ",
    "หน่วยงาน",
    "ผู้บันทึก",
    "วันที่เกิดเหตุ",
    "เวลาเกิดเหตุ",
    "กระบวนการที่เกิด",
    "ชื่อยา",
    "ระดับความรุนแรง",
    "รายละเอียดเหตุการณ์",
]


# =========================
# App Config (ENV)
# =========================
def get_app_config():
    return {
        "app_title": os.getenv("APP_TITLE", "Medication Error Logger"),
        "unit_name": os.getenv("UNIT_NAME", "ไม่ระบุหน่วยงาน"),
        "login_username": os.getenv("APP_LOGIN_USERNAME", "").strip(),
        "login_password": os.getenv("APP_LOGIN_PASSWORD", "").strip(),
    }


# =========================
# Helpers: secrets / config
# =========================
def _safe_get_st_secret(key, default=None):
    try:
        return st.secrets[key]
    except Exception:
        return default


def _normalize_private_key(creds: dict) -> dict:
    if "private_key" in creds and isinstance(creds["private_key"], str):
        creds["private_key"] = creds["private_key"].replace("\\n", "\n")
    return creds


def load_google_config():
    """
    Priority:
    1) st.secrets (local / Streamlit Cloud)
    2) ENV: GCP_SERVICE_ACCOUNT_JSON + GSHEET_URL + GSHEET_WORKSHEET
    3) ENV: GCP_SERVICE_ACCOUNT_FILE + GSHEET_URL + GSHEET_WORKSHEET
    """
    # 1) Streamlit secrets
    svc = _safe_get_st_secret("gcp_service_account", None)
    gsheet_cfg = _safe_get_st_secret("gsheets", None)
    if svc and gsheet_cfg:
        creds_dict = _normalize_private_key(dict(svc))
        spreadsheet_url = gsheet_cfg.get("spreadsheet_url", "").strip()
        worksheet_name = gsheet_cfg.get("worksheet", "MedicationError").strip() or "MedicationError"
        if not spreadsheet_url:
            raise ValueError("ไม่พบ gsheets.spreadsheet_url ใน secrets.toml")
        return creds_dict, spreadsheet_url, worksheet_name

    # 2) ENV JSON
    env_json = os.getenv("GCP_SERVICE_ACCOUNT_JSON", "").strip()
    spreadsheet_url = os.getenv("GSHEET_URL", "").strip()
    worksheet_name = os.getenv("GSHEET_WORKSHEET", "MedicationError").strip() or "MedicationError"

    if env_json and spreadsheet_url:
        creds_dict = json.loads(env_json)
        creds_dict = _normalize_private_key(creds_dict)
        return creds_dict, spreadsheet_url, worksheet_name

    # 3) ENV secret file
    creds_file = os.getenv("GCP_SERVICE_ACCOUNT_FILE", "").strip()
    if creds_file and spreadsheet_url:
        if not os.path.exists(creds_file):
            raise FileNotFoundError(f"ไม่พบไฟล์ credentials: {creds_file}")
        with open(creds_file, "r", encoding="utf-8") as f:
            creds_dict = json.load(f)
        creds_dict = _normalize_private_key(creds_dict)
        return creds_dict, spreadsheet_url, worksheet_name

    raise RuntimeError(
        "ยังไม่ได้ตั้งค่า Google credentials / Google Sheet\n"
        "- ตั้ง ENV: GCP_SERVICE_ACCOUNT_JSON + GSHEET_URL (+ GSHEET_WORKSHEET) หรือ\n"
        "- ตั้ง ENV: GCP_SERVICE_ACCOUNT_FILE + GSHEET_URL (+ GSHEET_WORKSHEET)\n"
        "- หรือใช้ .streamlit/secrets.toml ตอนรัน local"
    )


# =========================
# Google Sheets
# =========================
@st.cache_resource
def get_worksheet():
    creds_dict, spreadsheet_url, worksheet_name = load_google_config()
    gc = gspread.service_account_from_dict(creds_dict)
    sh = gc.open_by_url(spreadsheet_url)

    try:
        ws = sh.worksheet(worksheet_name)
    except gspread.exceptions.WorksheetNotFound:
        ws = sh.add_worksheet(title=worksheet_name, rows=2000, cols=20)

    ensure_headers(ws)
    return ws


def ensure_headers(ws):
    current_headers = ws.row_values(1)
    if current_headers[: len(HEADERS)] != HEADERS:
        ws.update(f"A1:I1", [HEADERS])


def append_incident(ws, config, reporter, event_date, event_time, process, drug_name, severity, details):
    now_str = datetime.now(THAI_TZ).strftime("%Y-%m-%d %H:%M:%S")
    row = [
        now_str,
        config["unit_name"],
        reporter,
        event_date.strftime("%Y-%m-%d"),
        event_time.strftime("%H:%M"),
        process,
        drug_name.strip(),
        severity,
        details.strip(),
    ]
    ws.append_row(row, value_input_option="USER_ENTERED")


@st.cache_data(ttl=10)
def load_records():
    ws = get_worksheet()
    records = ws.get_all_records(default_blank="")
    if not records:
        return pd.DataFrame(columns=HEADERS)

    df = pd.DataFrame(records)
    for col in HEADERS:
        if col not in df.columns:
            df[col] = ""

    df = df[HEADERS].copy()
    dt_text = df["วันที่เกิดเหตุ"].astype(str).str.strip() + " " + df["เวลาเกิดเหตุ"].astype(str).str.strip()
    df["_sort_dt"] = pd.to_datetime(dt_text, errors="coerce")
    df = df.sort_values("_sort_dt", ascending=False, na_position="last").drop(columns=["_sort_dt"])
    return df


# =========================
# Login (simple)
# =========================
def init_session():
    if "authenticated" not in st.session_state:
        st.session_state.authenticated = False
    if "login_user" not in st.session_state:
        st.session_state.login_user = ""
    if "login_error" not in st.session_state:
        st.session_state.login_error = ""


def login_required(config):
    """
    Simple login via ENV:
    APP_LOGIN_USERNAME / APP_LOGIN_PASSWORD
    """
    init_session()

    # ถ้าไม่ได้ตั้ง env login ไว้ ให้เข้าได้เลย (ใช้ตอน dev)
    if not config["login_username"] or not config["login_password"]:
        st.warning("ยังไม่ได้ตั้งค่า APP_LOGIN_USERNAME / APP_LOGIN_PASSWORD (โหมดไม่ล็อกอิน)")
        return True

    if st.session_state.authenticated:
        return True

    st.subheader("🔐 เข้าสู่ระบบ")
    st.caption(f"หน่วยงาน: {config['unit_name']}")

    with st.form("login_form", clear_on_submit=False):
        username = st.text_input("ชื่อผู้ใช้")
        password = st.text_input("รหัสผ่าน", type="password")
        submitted = st.form_submit_button("เข้าสู่ระบบ", use_container_width=True)

    if submitted:
        user_ok = secrets.compare_digest(username.strip(), config["login_username"])
        pass_ok = secrets.compare_digest(password, config["login_password"])

        if user_ok and pass_ok:
            st.session_state.authenticated = True
            st.session_state.login_user = username.strip()
            st.session_state.login_error = ""
            st.rerun()
        else:
            st.session_state.login_error = "ชื่อผู้ใช้หรือรหัสผ่านไม่ถูกต้อง"

    if st.session_state.login_error:
        st.error(st.session_state.login_error)

    return False


def logout_button():
    if st.session_state.get("authenticated"):
        if st.button("ออกจากระบบ"):
            st.session_state.authenticated = False
            st.session_state.login_user = ""
            st.session_state.login_error = ""
            st.rerun()


# =========================
# UI Tabs
# =========================
def render_form_tab(config):
    st.subheader("📝 บันทึกข้อมูลอุบัติการณ์")
    st.caption(f"หน่วยงาน: **{config['unit_name']}** | ผู้ใช้งาน: **{st.session_state.get('login_user','-')}**")

    col1, col2 = st.columns(2)
    with col1:
        event_date = st.date_input("วันที่เกิดเหตุ", value=date.today())
    with col2:
        default_time = datetime.now(THAI_TZ).time().replace(second=0, microsecond=0)
        event_time = st.time_input("เวลาเกิดเหตุ", value=default_time)

    process = st.selectbox("กระบวนการที่เกิด", PROCESS_OPTIONS)
    drug_name = st.text_input("ชื่อยา", placeholder="เช่น Warfarin / Insulin / Ceftriaxone")
    severity = st.selectbox("ระดับความรุนแรง", SEVERITY_OPTIONS)
    details = st.text_area("รายละเอียดเหตุการณ์", height=180, placeholder="กรอกรายละเอียดเหตุการณ์...")

    if st.button("บันทึกข้อมูล", type="primary", use_container_width=True):
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
                config=config,
                reporter=st.session_state.get("login_user", config["login_username"] or "unknown"),
                event_date=event_date,
                event_time=event_time,
                process=process,
                drug_name=drug_name,
                severity=severity,
                details=details,
            )
            load_records.clear()
            st.success("บันทึกข้อมูลสำเร็จ ✅")
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

    df_filter = df.copy()

    # แปลงวันที่ในชีตให้เป็น pandas datetime (ทนต่อข้อมูลผิดรูปแบบ)
    df_filter["_event_dt"] = pd.to_datetime(
        df_filter["วันที่เกิดเหตุ"].astype(str).str.strip(),
        errors="coerce"
    )

    # หา min/max วันที่สำหรับ default ของตัวกรอง (ต้องเป็น Python date)
    valid_dt = df_filter["_event_dt"].dropna()

    if len(valid_dt) == 0:
        min_d = date.today()
        max_d = date.today()
    else:
        min_d = valid_dt.min().date()
        max_d = valid_dt.max().date()

    st.markdown("### ตัวกรอง")
    c1, c2, c3, c4 = st.columns([1, 1, 1, 2])

    with c1:
        start_date = st.date_input("วันที่เริ่ม", value=min_d, key="hist_start")
    with c2:
        end_date = st.date_input("วันที่สิ้นสุด", value=max_d, key="hist_end")
    with c3:
        severity_filter = st.multiselect("ระดับความรุนแรง", SEVERITY_OPTIONS, default=[])
    with c4:
        keyword = st.text_input("ค้นหา (ชื่อยา/รายละเอียด)", key="hist_keyword")

    process_filter = st.multiselect("กระบวนการที่เกิด", PROCESS_OPTIONS, default=[])

    # แปลง date จาก widget -> pandas Timestamp เพื่อเทียบกับ _event_dt
    start_ts = pd.Timestamp(start_date)
    end_ts = pd.Timestamp(end_date) + pd.Timedelta(days=1) - pd.Timedelta(seconds=1)

    filtered = df_filter.copy()

    # กรองช่วงวันที่ (ให้แถวที่ parse วันที่ไม่ได้ยังแสดงได้ถ้าต้องการ)
    filtered = filtered[
        (filtered["_event_dt"].isna()) |
        ((filtered["_event_dt"] >= start_ts) & (filtered["_event_dt"] <= end_ts))
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

    # ลบคอลัมน์ช่วยก่อนแสดง
    filtered = filtered.drop(columns=["_event_dt"], errors="ignore")

    m1, m2, m3 = st.columns(3)
    m1.metric("จำนวนรายการทั้งหมด", f"{len(df):,}")
    m2.metric("ผลลัพธ์ตามตัวกรอง", f"{len(filtered):,}")
    m3.metric("ระดับ E-I", f"{filtered['ระดับความรุนแรง'].isin(['E','F','G','H','I']).sum():,}")

    st.dataframe(filtered, use_container_width=True, hide_index=True)


# =========================
# Main
# =========================
def main():
    config = get_app_config()

    st.set_page_config(
        page_title=config["app_title"],
        page_icon="🏡",
        layout="wide",
    )

    st.title("🏡 " + config["app_title"])
    st.caption("บันทึกอุบัติการณ์ในสถานพยาบาลปฐมภูมิ")

    # Login gate
    if not login_required(config):
        st.stop()

    # Header actions
    top1, top2 = st.columns([6, 1])
    with top1:
        st.markdown(f"**หน่วยงาน:** `{config['unit_name']}`")
    with top2:
        logout_button()

    # Connection status
    with st.expander("🔧 สถานะการเชื่อมต่อ", expanded=False):
        try:
            _, sheet_url, worksheet_name = load_google_config()
            st.success("ตั้งค่าเชื่อมต่อครบแล้ว")
            st.write(f"Worksheet: `{worksheet_name}`")
            st.write(f"Sheet URL: {sheet_url}")
        except Exception as e:
            st.error(str(e))

    tab1, tab2 = st.tabs(["บันทึกข้อมูล", "ดูข้อมูลย้อนหลัง"])
    with tab1:
        render_form_tab(config)
    with tab2:
        render_history_tab()


if __name__ == "__main__":
    main()
