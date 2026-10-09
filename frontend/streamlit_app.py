"""
MARG Pharmaceutical Enterprise Suite — Streamlit Web UI

Master Application Router:
- 🏠 Home Dashboard (Landing Page)
- 💊 Procurement Copilot (FEFO-aware replenishment & order approvals)
- 📈 Sales Intelligence (Customer reorder suggestions & financial dues)
"""
import requests
import streamlit as st
import pandas as pd
from pathlib import Path

from frontend.landing_page import render_landing_page
from frontend.procurement_page import render_procurement_page
from frontend.sales_page import render_sales_page

# Page config
st.set_page_config(
    page_title="MARG Pharma Enterprise Suite",
    page_icon="🏥",
    layout="wide",
    initial_sidebar_state="expanded"
)

# Custom Styling
st.markdown("""
<style>
    .main-header {
        font-size: 2.2rem;
        font-weight: 700;
        color: #1E293B;
        margin-bottom: 0.2rem;
    }
    .sub-header {
        color: #64748B;
        font-size: 1rem;
        margin-bottom: 1.5rem;
    }
    .badge-fefo-danger {
        background-color: #FEE2E2;
        color: #991B1B;
        padding: 4px 10px;
        border-radius: 6px;
        font-weight: 600;
        font-size: 0.82rem;
        display: inline-block;
        margin-bottom: 8px;
    }
    .badge-fefo-warn {
        background-color: #FEF3C7;
        color: #92400E;
        padding: 4px 10px;
        border-radius: 6px;
        font-weight: 600;
        font-size: 0.82rem;
        display: inline-block;
        margin-bottom: 8px;
    }
    .badge-fefo-ok {
        background-color: #DCFCE7;
        color: #166534;
        padding: 4px 10px;
        border-radius: 6px;
        font-weight: 600;
        font-size: 0.82rem;
        display: inline-block;
        margin-bottom: 8px;
    }
    .correction-box {
        background-color: #F8FAFC;
        border: 1px solid #E2E8F0;
        border-radius: 8px;
        padding: 12px;
        margin-top: 8px;
    }
    .stButton>button {
        border-radius: 8px;
        font-weight: 500;
    }
</style>
""", unsafe_allow_html=True)

# Backend URL configuration
BACKEND_URL = st.sidebar.text_input("Backend API URL", "http://127.0.0.1:8000")


def check_backend_health():
    for _ in range(2):
        try:
            r = requests.get(f"{BACKEND_URL}/health", timeout=5)
            if r.status_code == 200:
                return True, r.json()
        except Exception:
            pass
    return False, {}


is_healthy, health_info = check_backend_health()

# Initialize Navigation State
if "current_page" not in st.session_state:
    q_page = st.query_params.get("page")
    if q_page in ("landing", "procurement", "sales"):
        st.session_state["current_page"] = q_page
    else:
        st.session_state["current_page"] = "landing"

PAGE_MAP = {
    "landing": "🏠 Home Dashboard",
    "procurement": "💊 Procurement Copilot",
    "sales": "📈 Sales Intelligence",
}
REV_PAGE_MAP = {v: k for k, v in PAGE_MAP.items()}

current_key = st.session_state.get("current_page", "landing")
current_label = PAGE_MAP.get(current_key, "🏠 Home Dashboard")

# Keep sidebar_nav synchronized BEFORE widget instantiation
if "sidebar_nav" in st.session_state and st.session_state["sidebar_nav"] != current_label:
    st.session_state["sidebar_nav"] = current_label


def on_nav_change():
    selected_label = st.session_state.get("sidebar_nav")
    target_page = REV_PAGE_MAP.get(selected_label, "landing")
    st.session_state["current_page"] = target_page
    st.query_params["page"] = target_page


# Sidebar Navigation
st.sidebar.title("🧭 Workspace")
st.sidebar.radio(
    "Select Application",
    options=list(PAGE_MAP.values()),
    index=list(PAGE_MAP.keys()).index(current_key) if current_key in PAGE_MAP else 0,
    key="sidebar_nav",
    on_change=on_nav_change,
    label_visibility="collapsed"
)

st.sidebar.markdown("---")
st.sidebar.title("⚙️ System Status")
if is_healthy:
    st.sidebar.success(f"🟢 Connected to Backend\nMode: `{health_info.get('mode', 'dry_run')}`")
else:
    st.sidebar.error("🔴 Backend Disconnected\nPlease ensure FastAPI is running on port 8000.")

st.sidebar.markdown("---")
st.sidebar.title("🗑️ Database Management")
st.sidebar.caption("Clean-slate mode: Wipe all database records.")
if st.sidebar.button(
    "🗑️ Purge & Wipe Database",
    key="btn_sidebar_purge_db",
    type="secondary",
    use_container_width=True,
    help="Wipes all products, inventory batches, sales records, suppliers, proposals, and customer data to start 100% clean."
):
    try:
        p_res = requests.post(f"{BACKEND_URL}/system/reset-db", timeout=15)
        if p_res.status_code == 200:
            for k in [
                'uploader_version',
                'ordered_uploader_version',
                'sales_master_version',
                'sales_prod_version',
                'last_upload_stats',
                'last_upload_success_files',
                'ordered_items_upload_result',
                'sales_upload_banner',
                'sales_upload_error',
                'upload_banner',
            ]:
                st.session_state.pop(k, None)
            st.session_state['uploader_version'] = st.session_state.get('uploader_version', 0) + 1
            st.session_state['ordered_uploader_version'] = st.session_state.get('ordered_uploader_version', 0) + 1
            st.session_state['sales_master_version'] = st.session_state.get('sales_master_version', 0) + 1
            st.session_state['sales_prod_version'] = st.session_state.get('sales_prod_version', 0) + 1
            st.sidebar.success("✅ Database purged completely!")
            st.rerun()
        else:
            st.sidebar.error(f"Error resetting database: {p_res.text}")
    except Exception as e:
        st.sidebar.error(f"Reset error: {e}")

st.sidebar.markdown("---")

# Render active page based on current_page
active_page = st.session_state.get("current_page", "landing")

if active_page == "landing":
    render_landing_page(BACKEND_URL, is_healthy)
elif active_page == "procurement":
    render_procurement_page(BACKEND_URL, is_healthy, health_info)
elif active_page == "sales":
    render_sales_page(BACKEND_URL, is_healthy)
else:
    st.session_state["current_page"] = "landing"
    render_landing_page(BACKEND_URL, is_healthy)
