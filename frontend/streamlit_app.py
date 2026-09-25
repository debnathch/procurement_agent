"""
MARG Pharmaceutical Procurement Copilot — Streamlit Web UI

Features:
- Upload real MARG ERP Excel files (.xlsx / .xls)
- Download ready-to-test sample MARG Excel template
- Auto-extract Products, Batches, Expiries (FEFO), Sales, and Suppliers
- Run procurement agent to generate reorder suggestions with plain-English rationales
- Human-in-the-loop review: Approve, Modify Qty/Supplier, or Reject proposals
- Export completed/approved orders to CSV/Excel for MARG ERP or vendors
- Live FEFO inventory monitor and audit compliance log
"""
import io
import requests
import streamlit as st
import pandas as pd
import re
from pathlib import Path
from datetime import datetime, timedelta

try:
    from backend.app.core.config import settings
except ImportError:
    settings = None


def is_excluded_product(name: str) -> bool:
    """
    Excludes non-medicine and promotional items matching user-blacklisted keywords:
    'PACKING', 'PEN-', 'PILLOW', 'BAG-', 'BAG ', 'product card', 'visual aid', 'visiting', 'cylinder' and summary footer rows.
    """
    if not name:
        return True
    s_upper = str(name).strip().upper()
    for kw in ('PACKING', 'PEN-', 'PILLOW', 'BAG-', 'BAG '):
        if kw in s_upper:
            return True
    nl = s_upper.lower().replace('-', ' ')
    blacklist = ['product card', 'visual aid', 'visiting', 'cylinder']
    if any(b in nl for b in blacklist):
        return True
    if re.search(r'^\d+\s*items?$', nl.strip()):
        return True
    return False

# Page config
st.set_page_config(
    page_title="MARG Procurement Agent",
    page_icon="💊",
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
    try:
        r = requests.get(f"{BACKEND_URL}/health", timeout=3)
        return r.status_code == 200, r.json() if r.status_code == 200 else {}
    except Exception:
        return False, {}


is_healthy, health_info = check_backend_health()

# Sidebar Info
st.sidebar.title("⚙️ System Status")
if is_healthy:
    st.sidebar.success(f"🟢 Connected to Backend\nMode: `{health_info.get('mode', 'dry_run')}`")
else:
    st.sidebar.error("🔴 Backend Disconnected\nPlease ensure FastAPI is running on port 8000.")

st.sidebar.markdown("---")
st.sidebar.markdown("""
### 🛡️ Guardrails & Safety
- **Mode**: Dry-Run (No MARG PO without review)
- **FEFO Awareness**: 180-day expiry horizon
- **Pack Multiples**: Automatically preserved
- **Supplier Override**: Enabled for reviewers
""")

st.sidebar.markdown("---")
st.sidebar.markdown("### ⏱️ Coverage & Lead Time Policy")
config_lead_time = st.sidebar.number_input(
    "Default Lead Time (Days)",
    min_value=1,
    max_value=180,
    value=45,
    step=1,
    help="Configurable supplier delivery lead time used for coverage calculations"
)
config_review_days = st.sidebar.number_input("Review Cycle (Days)", min_value=1, max_value=30, value=7, step=1)
config_safety_days = st.sidebar.number_input("Safety Buffer (Days)", min_value=0, max_value=30, value=3, step=1)
total_coverage = int(config_lead_time + config_review_days + config_safety_days)
st.sidebar.info(f"📊 Total Target Coverage: **{total_coverage} days** ({int(config_lead_time)}d lead + {int(config_review_days)}d review + {int(config_safety_days)}d safety)")
st.sidebar.caption("🛡️ **FEFO Expiry Horizon**: 180 days (batches expiring within 6 months flagged)")

# Title & Header
st.markdown('<div class="main-header">💊 MARG Procurement Copilot</div>', unsafe_allow_html=True)
st.markdown('<div class="sub-header">Upload MARG ERP Excel, generate FEFO-aware procurement recommendations, and correct/approve suggested orders.</div>', unsafe_allow_html=True)


# Fetch suppliers and proposals for the page
suppliers_list = []
proposals = []
if is_healthy:
    try:
        s_res = requests.get(f"{BACKEND_URL}/suppliers", timeout=5)
        if s_res.status_code == 200:
            suppliers_list = s_res.json()
    except Exception:
        suppliers_list = []

    try:
        res = requests.get(f"{BACKEND_URL}/proposals", timeout=5)
        if res.status_code == 200:
            proposals = res.json()
    except Exception:
        proposals = []

# Top metrics
valid_proposals = [p for p in proposals if not is_excluded_product(p.get('product_name', ''))]
pending = [p for p in valid_proposals if p.get('status') == 'PENDING']
approved = [p for p in valid_proposals if p.get('status') in ('APPROVED_PENDING_EXECUTION', 'EXECUTED')]
total_pending_val = sum(p.get('estimated_value', 0) for p in pending)
high_expiry_items = sum(1 for p in pending if p.get('expiry_risk_score', 0) >= 0.25)

col_m1, col_m2, col_m3, col_m4 = st.columns(4)
with col_m1:
    st.metric("Pending Orders for Review", len(pending))
with col_m2:
    st.metric("Proposed Purchase Value", f"₹{total_pending_val:,.2f}")
with col_m3:
    st.metric("Expiry Risk Alerts", high_expiry_items)
with col_m4:
    st.metric("Approved Orders", len(approved))

st.markdown("---")

# Main Tabs
tab_upload, tab_proposals, tab_approved, tab_no_reorder, tab_inventory, tab_audit, tab_logs = st.tabs([
    "📂 Upload MARG Excel & Run",
    "💡 Review & Correct Suggestions",
    "📦 Approved Orders / PO Export",
    "🛡️ No Need for Reorder",
    "📊 Inventory & FEFO Expiry",
    "📜 Compliance Audit Log",
    "📋 Live Rolling Logs"
])

# ---------------------------------------------------------------------------
# TAB 1: Upload MARG Excel & Run Agent
# ---------------------------------------------------------------------------
with tab_upload:
    st.subheader("1. Feed MARG ERP Excel Export")
    st.markdown("""
    Feed your MARG Excel report (`.xlsx` or `.xls`) into the agent. It automatically extracts:
    - **Closing Stock & Batches** (Item code, pack, batch number, closing quantity, purchase rate)
    - **FEFO Expiry Dates** (`MM/YY`, `12/26`, `DD/MM/YYYY`)
    - **Sales Velocity** (historical daily/monthly demand)
    - **Suppliers & Lead Times**
    """)

    col_up1, col_up2 = st.columns([2, 1])

    with col_up2:
        st.info("💡 **Need a test file?**\nDownload our sample MARG Excel workbook with realistic pharmaceutical stock, batches, and sales:")
        if is_healthy:
            try:
                tpl_res = requests.get(f"{BACKEND_URL}/ingestion/sample-template", timeout=5)
                if tpl_res.status_code == 200:
                    st.download_button(
                        label="📥 Download Sample MARG Excel",
                        data=tpl_res.content,
                        file_name="marg_sample_data.xlsx",
                        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        use_container_width=True
                    )
            except Exception as e:
                st.warning(f"Could not fetch sample template: {e}")

        st.markdown("---")
        st.write("##### 🗑️ Database Management")
        st.caption("Clean-slate mode: Wipe previous database records at any time before a new run.")
        if st.button("Purge & Wipe Database Now", type="secondary", use_container_width=True, help="Wipes all inventory, sales, suppliers, and proposals to start 100% clean"):
            try:
                p_res = requests.post(f"{BACKEND_URL}/system/reset-db", timeout=10)
                if p_res.status_code == 200:
                    st.success("✅ Database purged completely! Historical data cleared.")
                    st.rerun()
                else:
                    st.error(f"Error resetting database: {p_res.text}")
            except Exception as e:
                st.error(f"Reset error: {e}")

    with col_up1:
        uploaded_files = st.file_uploader(
            "Select MARG Export file(s) (.xlsx, .xls, or .csv) — You can select multiple files at once!",
            type=["xlsx", "xls", "csv"],
            accept_multiple_files=True,
            help="Upload raw MARG ERP exports directly without reformatting (Closing Stock, Manufacturer List, PCD Outstanding, etc.)"
        )

        col_opt1, col_opt2 = st.columns([1, 1])
        with col_opt1:
            auto_run = st.checkbox("Automatically run Procurement Agent after ingestion", value=True)
        with col_opt2:
            clear_db_first = st.checkbox(
                "🧹 Refresh Database (wipe old data before import)",
                value=True,
                help="Recommended: Clears all existing products, inventory batches, suppliers, and past suggestions so only the fresh data remains in the database."
            )

        if uploaded_files:
            st.success(f"📁 Loaded **{len(uploaded_files)}** file(s): `{'`, `'.join([f.name for f in uploaded_files])}`")

            # Preview each file
            for f in uploaded_files:
                try:
                    if f.name.lower().endswith('.csv'):
                        df_preview = pd.read_csv(f, nrows=5)
                        with st.expander(f"👀 Preview '{f.name}' (CSV Format)", expanded=(len(uploaded_files) == 1)):
                            st.dataframe(df_preview, use_container_width=True)
                    else:
                        xl = pd.ExcelFile(f)
                        with st.expander(f"👀 Preview '{f.name}' (Sheets: {', '.join(xl.sheet_names)})", expanded=(len(uploaded_files) == 1)):
                            df_preview = xl.parse(xl.sheet_names[0], nrows=5)
                            st.dataframe(df_preview, use_container_width=True)
                except Exception as e:
                    st.warning(f"Preview note for {f.name}: {e}")

            if st.button("🚀 Ingest Data & Generate Procurement Suggestions", type="primary", use_container_width=True):
                if not is_healthy:
                    st.error("Cannot connect to backend. Please ensure FastAPI backend is running.")
                else:
                    with st.spinner("Analyzing MARG files, updating inventory & computing FEFO procurement suggestions..."):
                        cumulative_stats = {
                            'products_upserted': 0,
                            'batches_inserted': 0,
                            'suppliers_upserted': 0,
                            'sales_inserted': 0,
                        }
                        success_files = 0

                        for idx, f in enumerate(uploaded_files):
                            is_first = (idx == 0)
                            is_last = (idx == len(uploaded_files) - 1)
                            should_run_agent = (auto_run and is_last)
                            should_clear = (clear_db_first and is_first)

                            files = {'file': (f.name, f.getvalue(), f.type)}
                            try:
                                upload_res = requests.post(
                                    f"{BACKEND_URL}/ingestion/upload-marg-excel?run_agent={'true' if should_run_agent else 'false'}&lead_time_days={int(config_lead_time)}&clear_existing={'true' if should_clear else 'false'}",
                                    files=files,
                                    timeout=300
                                )
                                if upload_res.status_code == 200:
                                    success_files += 1
                                    res_json = upload_res.json()
                                    stats = res_json.get('stats', {})
                                    for k in cumulative_stats:
                                        cumulative_stats[k] += stats.get(k, 0)
                                    
                                    if should_run_agent and res_json.get('agent_run'):
                                        agent_run = res_json['agent_run']
                                        num_proposals = agent_run.get('proposals_count', 0)
                                        st.balloons()
                                        st.success(f"🎉 **Ingestion & Analysis Complete!** Generated **{num_proposals}** procurement recommendations across {success_files} file(s).")
                                else:
                                    st.error(f"Upload failed for {f.name}: {upload_res.text}")
                            except Exception as exc:
                                st.error(f"Error processing {f.name}: {exc}")

                        if success_files > 0:
                            st.success(f"✅ Ingested {success_files} file(s) into database:")
                            col_s1, col_s2, col_s3, col_s4 = st.columns(4)
                            col_s1.metric("Products Updated", cumulative_stats['products_upserted'])
                            col_s2.metric("Batches Recorded", cumulative_stats['batches_inserted'])
                            col_s3.metric("Suppliers Updated", cumulative_stats['suppliers_upserted'])
                            col_s4.metric("Sales Rows Imported", cumulative_stats['sales_inserted'])
                            st.info("👉 Switch to the **'Review & Correct Suggestions'** tab to review, adjust, and approve order suggestions!")


# ---------------------------------------------------------------------------
# TAB 2: Review & Correct Order Suggestions
# ---------------------------------------------------------------------------
with tab_proposals:
    st.subheader("2. Human-in-the-Loop: Review, Correct & Approve Suggestions")
    st.markdown("""
    The agent computes recommended quantities based on sales velocity, lead times, safety buffers, and FEFO expiry risk.
    **You have full control to correct any value (quantity, supplier, notes) before approving.**
    """)

    col_ref, col_filt, col_risk, col_search = st.columns([1, 1.5, 2, 2.5])
    with col_ref:
        if st.button("🔄 Refresh Data", key="refresh_proposals_btn"):
            st.rerun()

    with col_filt:
        status_filter = st.selectbox("Filter Status", ["PENDING", "APPROVED_PENDING_EXECUTION", "EXECUTED", "REJECTED", "ALL"])

    with col_risk:
        risk_filter = st.selectbox(
            "Expiry Risk Filter",
            ["All Risk Levels", "⚠️ Near-Expiry / Risk Products Only", "⛔ High Risk Only (PAUSE/REDUCE)"]
        )

    # Fetch proposals
    current_proposals = []
    if is_healthy:
        try:
            url = f"{BACKEND_URL}/proposals"
            if status_filter != "ALL":
                url += f"?status_filter={status_filter}"
            p_res = requests.get(url, timeout=10)
            if p_res.status_code == 200:
                current_proposals = p_res.json()
        except Exception as e:
            st.error(f"Error loading proposals: {e}")

    with col_search:
        search_kw = st.text_input("🔍 Search by Product or Code", "", key="proposal_search_kw")

    # Filter out blacklisted non-medicine items
    current_proposals = [p for p in current_proposals if not is_excluded_product(p.get('product_name', ''))]

    # Apply Expiry Risk Filter
    if risk_filter == "⚠️ Near-Expiry / Risk Products Only":
        current_proposals = [
            p for p in current_proposals
            if p.get('expiry_risk_score', 0) >= 0.25 or p.get('near_expiry_qty', 0) > 0
        ]
    elif risk_filter == "⛔ High Risk Only (PAUSE/REDUCE)":
        current_proposals = [
            p for p in current_proposals
            if p.get('expiry_risk_score', 0) >= 0.50
        ]

    # Sort alphabetically by product name (A-Z)
    current_proposals.sort(key=lambda p: str(p.get('product_name', '')).strip().upper())

    if search_kw:
        current_proposals = [
            p for p in current_proposals
            if search_kw.lower() in p.get('product_name', '').lower() or search_kw.lower() in p.get('product_code', '').lower()
        ]

    if not current_proposals:
        st.info("No proposals found for the selected filter. Upload a MARG Excel or click below to trigger a run:")
        if st.button("⚡ Run Procurement Agent Now", key="trigger_run_btn"):
            if is_healthy:
                r_res = requests.post(f"{BACKEND_URL}/runs", json={"lead_time_days": int(config_lead_time)}, timeout=30)
                if r_res.status_code == 201:
                    st.success("Procurement Run finished!")
                    st.rerun()
                else:
                    st.error(f"Run failed: {r_res.text}")
    else:
        pending_in_view = [p for p in current_proposals if p['status'] == 'PENDING']

        # Bulk Approval & Summary Actions Bar
        col_sum1, col_sum2 = st.columns([3, 2])
        with col_sum1:
            st.markdown(f"##### Showing **{len(current_proposals)}** total proposals ({len(pending_in_view)} pending review)")
        with col_sum2:
            if pending_in_view:
                if st.button(f"⚡ Bulk Approve All Pending ({len(pending_in_view)} Items)", type="primary", use_container_width=True, help="Approve all currently pending proposals at their suggested quantities in one step"):
                    with st.spinner(f"Approving {len(pending_in_view)} proposals..."):
                        try:
                            b_res = requests.post(
                                f"{BACKEND_URL}/proposals/batch-decide",
                                json={"action": "approve", "actor": "human-reviewer", "reason": "Bulk approved by procurement manager"},
                                timeout=30
                            )
                            if b_res.status_code == 200:
                                res_data = b_res.json()
                                st.success(f"🎉 Successfully approved {res_data.get('processed', 0)} purchase orders! Switch to Tab 3 to view & export POs.")
                                st.rerun()
                            else:
                                st.error(f"Bulk approval error: {b_res.text}")
                        except Exception as exc:
                            st.error(f"Batch request error: {exc}")

        # Summary Table view
        with st.expander("📊 Proposal Summary Table (Click to expand / collapse)", expanded=False):
            df_prop_summary = pd.DataFrame([
                {
                    'Product Code': p['product_code'],
                    'Product Name': p['product_name'],
                    'Recommended Qty': p['recommended_qty'],
                    'Unit Cost (₹)': p['unit_cost'],
                    'Total Value (₹)': p['estimated_value'],
                    'Avg Daily Demand': p['avg_daily_demand'],
                    'Stock on Hand': p['stock_on_hand'],
                    'Expiry Risk': f"{p.get('expiry_risk_score', 0):.0%}",
                    'Status': p['status'],
                }
                for p in current_proposals
            ])
            st.dataframe(df_prop_summary, use_container_width=True)

        # Refresh supplier lookup options from backend so fresh uploads are immediately reflected
        if is_healthy:
            try:
                s_res = requests.get(f"{BACKEND_URL}/suppliers", timeout=5)
                if s_res.status_code == 200:
                    suppliers_list = s_res.json()
            except Exception:
                pass

        # Sort suppliers alphabetically by supplier name
        valid_suppliers = [
            s for s in suppliers_list
            if s.get('supplier_name') and str(s.get('supplier_name', '')).strip().upper() not in (
                'BILL DATE', 'SUPPLIER', 'SUPPLIER NAME', 'TOTAL', 'NAN', 'NONE'
            )
        ]
        valid_suppliers.sort(key=lambda s: str(s.get('supplier_name', '')).strip().upper())
        supplier_options = {s['supplier_id']: s['supplier_name'] for s in valid_suppliers}
        if not supplier_options:
            supplier_options = {"SUP-DEFAULT": "Default Trade Supplier"}

        # Pagination for individual proposal review to ensure fast UI performance
        items_per_page = 20
        total_pages = max(1, (len(current_proposals) + items_per_page - 1) // items_per_page)

        col_p1, col_p2, col_p3 = st.columns([1, 2, 1])
        with col_p2:
            page = st.number_input("Page", min_value=1, max_value=total_pages, value=1, step=1, key="proposal_page_num")
            st.caption(f"Showing page {page} of {total_pages} ({len(current_proposals)} items)")

        start_idx = (page - 1) * items_per_page
        end_idx = min(start_idx + items_per_page, len(current_proposals))
        page_proposals = current_proposals[start_idx:end_idx]

        for p in page_proposals:
            p_id = p['id']
            st_color = "orange" if p['status'] == 'PENDING' else ("green" if 'APPROVED' in p['status'] or p['status'] == 'EXECUTED' else "red")

            # Expiry risk badge
            risk = p.get('expiry_risk_score', 0)
            if risk >= 0.50:
                risk_badge = f'<span class="badge-fefo-danger">⛔ High Expiry Risk ({risk:.0%}) — Action: {p.get("expiry_action")}</span>'
            elif risk >= 0.25:
                risk_badge = f'<span class="badge-fefo-warn">⚠️ Moderate Expiry Risk ({risk:.0%}) — Action: {p.get("expiry_action")}</span>'
            else:
                risk_badge = f'<span class="badge-fefo-ok">✅ Stock Shelf-Life Healthy (FEFO Clean)</span>'

            with st.expander(
                f"**{p['product_name']}** (`{p['product_code']}`) — Suggested: **{p['recommended_qty']:g} units** (₹{p['estimated_value']:,.2f}) | Status: :{st_color}[{p['status']}]",
                expanded=(p['status'] == 'PENDING' and len(page_proposals) <= 5)
            ):
                st.markdown(risk_badge, unsafe_allow_html=True)
                st.markdown(f"**Agent Rationale:** {p.get('rationale', 'N/A')}")

                # Metrics row
                c1, c2, c3, c4, c5 = st.columns(5)
                c1.metric("Stock on Hand", f"{p.get('stock_on_hand', 0):g}")
                c2.metric("Stock on Order", f"{p.get('stock_on_order', 0):g}")
                c3.metric("Near-Expiry Stock", f"{p.get('near_expiry_qty', 0):g}")
                c4.metric("Avg Daily Demand", f"{p.get('avg_daily_demand', 0):.2f}/day")
                c5.metric("Lead Time", f"{p.get('lead_time_days', int(config_lead_time))} days")

                unit_cost = p.get('unit_cost', 0)

                if p['status'] == 'PENDING':
                    st.markdown("---")
                    st.markdown("#### ✍️ Human Correction & Approval Panel")
                    st.caption("You can adjust the quantity, switch supplier, or write a custom justification before approving.")

                    col_edit1, col_edit2 = st.columns(2)

                    with col_edit1:
                        # Correct quantity
                        init_qty = max(0.0, float(p.get('recommended_qty', 0.0)))
                        corrected_qty = st.number_input(
                            f"Order Quantity (Suggested: {p['recommended_qty']:g})",
                            min_value=0.0,
                            value=init_qty,
                            step=1.0,
                            key=f"qty_input_{p_id}"
                        )
                        calculated_val = corrected_qty * unit_cost
                        st.write(f"💵 **Updated Total Value:** ₹{calculated_val:,.2f} *(at ₹{unit_cost:.2f} per unit)*")

                    with col_edit2:
                        # Correct supplier safely
                        current_sup_id = p.get('supplier_id')
                        current_sup_name = p.get('supplier_name')
                        if current_sup_id and current_sup_id not in supplier_options:
                            supplier_options[current_sup_id] = current_sup_name or current_sup_id
                        sup_keys = list(supplier_options.keys())
                        default_idx = sup_keys.index(current_sup_id) if (current_sup_id and current_sup_id in sup_keys) else 0

                        corrected_supplier_id = st.selectbox(
                            "Supplier",
                            options=sup_keys,
                            format_func=lambda sid: supplier_options.get(sid, sid),
                            index=default_idx,
                            key=f"sup_select_{p_id}"
                        )

                        # Decision note / reason
                        is_modified = (corrected_qty != p['recommended_qty']) or (corrected_supplier_id != p.get('supplier_id'))
                        default_note = "Modified by human reviewer" if is_modified else "Approved as recommended"
                        human_note = st.text_input(
                            "Reviewer Note / Justification",
                            value=default_note,
                            key=f"note_input_{p_id}"
                        )

                    col_btn1, col_btn2 = st.columns(2)

                    with col_btn1:
                        btn_label = f"✅ Approve with Corrections ({corrected_qty:g} units — ₹{calculated_val:,.2f})" if is_modified else f"✅ Approve as Recommended ({corrected_qty:g} units)"
                        if st.button(btn_label, key=f"btn_app_{p_id}", type="primary", use_container_width=True):
                            with st.spinner("Submitting approval..."):
                                decide_res = requests.post(
                                    f"{BACKEND_URL}/proposals/{p_id}/decide",
                                    json={
                                        "action": "approve",
                                        "approved_qty": corrected_qty,
                                        "supplier_id": corrected_supplier_id,
                                        "reason": human_note,
                                        "actor": "human-reviewer"
                                    },
                                    timeout=10
                                )
                                if decide_res.status_code == 200:
                                    st.success(f"Proposal for {p['product_name']} successfully approved!")
                                    st.rerun()
                                else:
                                    err_msg = decide_res.json().get('detail', decide_res.text)
                                    st.error(f"Approval blocked by guardrail: {err_msg}")

                    with col_btn2:
                        reject_note = st.text_input("Rejection Reason", "Not needed this cycle", key=f"rej_note_{p_id}")
                        if st.button("❌ Reject Proposal", key=f"btn_rej_{p_id}", use_container_width=True):
                            with st.spinner("Rejecting proposal..."):
                                decide_res = requests.post(
                                    f"{BACKEND_URL}/proposals/{p_id}/decide",
                                    json={
                                        "action": "reject",
                                        "reason": reject_note,
                                        "actor": "human-reviewer"
                                    },
                                    timeout=10
                                )
                                if decide_res.status_code == 200:
                                    st.warning(f"Proposal {p_id} rejected.")
                                    st.rerun()
                                else:
                                    st.error(f"Rejection error: {decide_res.text}")

                else:
                    # Proposal already approved or rejected
                    st.markdown("---")
                    st.markdown("##### 📋 Decision Record")
                    res_c1, res_c2, res_c3 = st.columns(3)
                    res_c1.write(f"**Approved Qty:** `{p.get('approved_qty')}`")
                    res_c2.write(f"**Assigned Supplier:** `{p.get('supplier_name')}`")
                    res_c3.write(f"**PO Reference:** `{p.get('execution_reference') or 'N/A'}`")
                    st.info(f"📝 **Reviewer Reason:** {p.get('human_reason') or 'None specified'}")


# ---------------------------------------------------------------------------
# TAB 3: Approved Orders / Export
# ---------------------------------------------------------------------------
with tab_approved:
    st.subheader("3. Finalized & Approved Purchase Orders")
    st.markdown("All proposals that have been verified, corrected, and approved by the human reviewer.")

    # Dynamically fetch latest approved orders from backend
    approved_proposals = []
    if is_healthy:
        try:
            a_res = requests.get(f"{BACKEND_URL}/proposals?status_filter=APPROVED_PENDING_EXECUTION", timeout=5)
            e_res = requests.get(f"{BACKEND_URL}/proposals?status_filter=EXECUTED", timeout=5)
            approved_proposals = (a_res.json() if a_res.status_code == 200 else []) + (e_res.json() if e_res.status_code == 200 else [])
        except Exception:
            approved_proposals = [p for p in proposals if p.get('status') in ('APPROVED_PENDING_EXECUTION', 'EXECUTED')]
    else:
        approved_proposals = [p for p in proposals if p.get('status') in ('APPROVED_PENDING_EXECUTION', 'EXECUTED')]

    # Filter blacklisted items and sort alphabetically
    approved_proposals = [p for p in approved_proposals if not is_excluded_product(p.get('product_name', ''))]
    approved_proposals.sort(key=lambda p: str(p.get('product_name', '')).strip().upper())

    if not approved_proposals:
        st.info("No approved orders yet. Review and approve proposals in the 'Review & Correct Suggestions' tab.")
    else:
        total_po_val = sum((p.get('approved_qty') or 0) * (p.get('unit_cost') or 0) for p in approved_proposals)
        col_ap1, col_ap2, col_ap3 = st.columns(3)
        col_ap1.metric("Total Approved Orders", len(approved_proposals))
        col_ap2.metric("Total Order Value", f"₹{total_po_val:,.2f}")
        exec_mode = getattr(settings, 'execution_mode', 'DRY_RUN').upper() if settings else 'DRY_RUN'
        col_ap3.metric("Execution Mode", exec_mode)

        df_approved = pd.DataFrame([
            {
                'Product Code': p['product_code'],
                'Product Name': p['product_name'],
                'Original Suggested Qty': p['recommended_qty'],
                'Final Approved Qty': p['approved_qty'],
                'Unit Cost (₹)': p['unit_cost'],
                'Total Value (₹)': (p['approved_qty'] or 0) * (p['unit_cost'] or 0),
                'Supplier': p['supplier_name'],
                'Reviewer Note': p['human_reason'],
                'PO Reference': p['execution_reference'],
                'Status': p['status'],
                'Approved At': p['approved_at'],
            }
            for p in approved_proposals
        ])

        st.dataframe(df_approved, use_container_width=True)

        col_exp1, col_exp2 = st.columns([1, 4])
        with col_exp1:
            csv_data = df_approved.to_csv(index=False).encode('utf-8')
            st.download_button(
                label="📥 Export Orders to CSV",
                data=csv_data,
                file_name="approved_purchase_orders.csv",
                mime="text/csv",
                use_container_width=True
            )


# ---------------------------------------------------------------------------
# TAB: No Need for Reorder (Net Need <= 0)
# ---------------------------------------------------------------------------
with tab_no_reorder:
    st.subheader("🛡️ Products with No Need for Reorder (Net Need ≤ 0)")
    st.markdown("""
    These pharmaceutical products currently have **sufficient usable stock on hand and pending orders** to cover forecasted customer demand across the entire delivery lead time, review cycle, and safety buffer.
    """)

    # Fetch no-reorder list directly from backend (runs in ~20ms, always live)
    no_reorder_items = []
    if is_healthy:
        try:
            nr_res = requests.get(
                f"{BACKEND_URL}/procurement/no-reorder",
                params={"lead_time_days": int(config_lead_time)},
                timeout=20
            )
            if nr_res.status_code == 200:
                no_reorder_items = nr_res.json()
        except Exception as e:
            st.warning(f"Could not load no-reorder data: {e}")

    # Exclude blacklisted non-medicine items (PACKING, PEN-, PILLOW, BAG-, etc.)
    no_reorder_items = [p for p in no_reorder_items if not is_excluded_product(p.get('product_name', ''))]

    # Total list number
    total_no_reorder_count = len(no_reorder_items)

    # Top Metrics Bar
    if total_no_reorder_count > 0:
        total_healthy_val = sum(p.get('inventory_value', 0.0) for p in no_reorder_items)
        avg_cov_days = sum(min(p.get('coverage_days', 0.0), 365.0) for p in no_reorder_items) / total_no_reorder_count
        zero_risk_count = sum(1 for p in no_reorder_items if p.get('expiry_risk_score', 0.0) == 0.0)

        col_nr1, col_nr2, col_nr3, col_nr4 = st.columns(4)
        with col_nr1:
            st.metric("Total Products Not Needing Reorder", total_no_reorder_count)
        with col_nr2:
            st.metric("Capital in Healthy Stock", f"₹{total_healthy_val:,.2f}")
        with col_nr3:
            st.metric("Avg Inventory Coverage", f"{avg_cov_days:.1f} days")
        with col_nr4:
            st.metric("100% FEFO Healthy", f"{zero_risk_count / total_no_reorder_count:.0%}")

    st.markdown("---")

    # Search Bar with dedicated Search and Reset Buttons
    st.markdown("##### 🔍 Search & Filter Products")
    col_s1, col_s2, col_s3 = st.columns([3, 1, 1])

    with col_s1:
        search_kw = st.text_input(
            "Search product by name or item code",
            value="",
            placeholder="Type medicine name or code (e.g. GINIPLEX, AC-BEN, MED-...)",
            label_visibility="collapsed",
            key="input_search_no_reorder"
        ).strip().lower()

    with col_s2:
        st.button("🔍 Search Product", type="primary", use_container_width=True, key="btn_do_search_no_reorder")

    with col_s3:
        if st.button("🔄 Show All Products", use_container_width=True, key="btn_clear_search_no_reorder"):
            search_kw = ""
            st.rerun()

    # Active search filter
    if search_kw:
        display_items = [
            p for p in no_reorder_items
            if search_kw in p.get('product_name', '').lower() or search_kw in p.get('product_code', '').lower()
        ]
    else:
        display_items = list(no_reorder_items)

    # Ensure ordered by character (alphabetical order A-Z by product_name)
    display_items.sort(key=lambda p: str(p.get('product_name', '')).strip().upper())

    # Total List Number Display
    if search_kw:
        st.info(f"📋 **Total List Count**: Showing **{len(display_items)}** products matching `'{search_kw}'` (out of **{total_no_reorder_count}** total products with Net Need ≤ 0).")
    else:
        st.info(f"📋 **Total List Count**: Showing all **{total_no_reorder_count}** products with Net Need ≤ 0 in alphabetical order (A–Z). No reorder needed.")

    if not display_items:
        if search_kw:
            st.warning(f"No products found matching '{search_kw}'. Try a different keyword or click 'Show All Products'.")
        else:
            st.info("No products currently have Net Need ≤ 0. Run the procurement agent after uploading stock and sales data.")
    else:
        # Direct summary table view (always visible)
        df_no_reorder = pd.DataFrame([
            {
                'Product Name': p['product_name'],
                'Product Code': p['product_code'],
                'Stock on Hand': p['stock_on_hand'],
                'Usable Stock (FEFO)': p['usable_before_expiry'],
                'On Order': p['stock_on_order'],
                'Daily Demand': p['avg_daily_demand'],
                'Target Required': p['target_stock'],
                'Net Need': f"{p['net_need']:.2f}",
                'Surplus Units': f"+{p['surplus_qty']:.2f}",
                'Coverage (Days)': f"{p['coverage_days']:.1f}d" if p['coverage_days'] < 999 else "No Demand",
                'Unit Cost (₹)': p['unit_cost'],
                'Total Stock Value (₹)': p['inventory_value'],
                'Status': "✅ Covered (No Reorder)",
            }
            for p in display_items
        ])

        st.dataframe(df_no_reorder, use_container_width=True, hide_index=True)

        col_dn1, col_dn2 = st.columns([1, 4])
        with col_dn1:
            csv_nr = df_no_reorder.to_csv(index=False).encode('utf-8')
            st.download_button(
                label="📥 Download CSV List",
                data=csv_nr,
                file_name="products_no_need_for_reorder.csv",
                mime="text/csv",
                use_container_width=True
            )

        # Pagination for individual product review
        items_per_page_nr = 25
        total_pages_nr = max(1, (len(display_items) + items_per_page_nr - 1) // items_per_page_nr)

        col_pg1, col_pg2, col_pg3 = st.columns([1, 2, 1])
        with col_pg2:
            page_nr = st.number_input(
                "Page",
                min_value=1,
                max_value=total_pages_nr,
                value=1,
                step=1,
                key="no_reorder_page_num"
            )
            st.caption(f"Showing page {page_nr} of {total_pages_nr} ({len(display_items)} products in alphabetical order)")

        start_idx_nr = (page_nr - 1) * items_per_page_nr
        end_idx_nr = min(start_idx_nr + items_per_page_nr, len(display_items))
        page_items_nr = display_items[start_idx_nr:end_idx_nr]

        for p in page_items_nr:
            with st.expander(
                f"**{p['product_name']}** (`{p['product_code']}`) — Stock on Hand: **{p['stock_on_hand']:g} units** (Surplus: +{p['surplus_qty']:g} units) | Net Need: **{p['net_need']:g}**",
                expanded=False
            ):
                st.markdown(f"**Agent Rationale:** {p['rationale']}")
                c1, c2, c3, c4, c5 = st.columns(5)
                c1.metric("Stock on Hand", f"{p['stock_on_hand']:g}")
                c2.metric("Usable (FEFO)", f"{p['usable_before_expiry']:g}")
                c3.metric("Stock on Order", f"{p['stock_on_order']:g}")
                c4.metric("Avg Daily Demand", f"{p['avg_daily_demand']:.2f}/day")
                c5.metric("Target Stock Needed", f"{p['target_stock']:g}")

                c6, c7, c8, c9 = st.columns(4)
                c6.metric("Net Need", f"{p['net_need']:g} units", delta=f"{p['net_need']:g} (No Reorder)")
                c7.metric("Surplus Stock", f"+{p['surplus_qty']:g} units")
                c8.metric("Days Coverage", f"{p['coverage_days']:.1f} days" if p['coverage_days'] < 999 else "No Demand")
                c9.metric("Stock Capital", f"₹{p['inventory_value']:,.2f}")


# ---------------------------------------------------------------------------
# TAB 4: Current Inventory & FEFO Expiry
# ---------------------------------------------------------------------------
with tab_inventory:
    st.subheader("4. Inventory Batches & FEFO Expiry Status")
    if is_healthy:
        try:
            inv_res = requests.get(f"{BACKEND_URL}/inventory", timeout=5)
            if inv_res.status_code == 200 and inv_res.json():
                df_inv = pd.DataFrame(inv_res.json())

                # Ensure product_name is available, fallback to product_code if empty
                if 'product_name' not in df_inv.columns:
                    df_inv['product_name'] = df_inv.get('product_code', '')
                else:
                    df_inv['product_name'] = df_inv['product_name'].fillna(df_inv.get('product_code', ''))

                # Filter blacklisted items and sort alphabetically A-Z
                df_inv = df_inv[~df_inv['product_name'].apply(is_excluded_product)]
                df_inv = df_inv.sort_values(by='product_name', ascending=True)

                # Compute FEFO Expiry Status for every batch
                now_dt = datetime.utcnow()
                horizon_days = int(settings.expiry_risk_horizon_days if settings else 180)
                horizon_dt = now_dt + timedelta(days=horizon_days)

                def calc_batch_fefo(exp_val):
                    if not exp_val or pd.isna(exp_val):
                        return "Unknown / General"
                    try:
                        exp_dt = pd.to_datetime(exp_val)
                        if exp_dt <= now_dt:
                            return "⛔ Expired"
                        elif exp_dt <= horizon_dt:
                            return f"⚠️ Near-Expiry (≤ {horizon_days}d)"
                        else:
                            return "✅ Shelf-Life Healthy"
                    except Exception:
                        return "Unknown"

                df_inv['FEFO Status'] = df_inv['expiry_date'].apply(calc_batch_fefo)

                # Format Expiry Date cleanly for display (YYYY-MM-DD or MM/YYYY)
                def format_exp_date(exp_val):
                    if not exp_val or pd.isna(exp_val):
                        return "N/A"
                    try:
                        exp_dt = pd.to_datetime(exp_val)
                        return exp_dt.strftime("%Y-%m-%d")
                    except Exception:
                        return str(exp_val)

                df_inv['Formatted Expiry'] = df_inv['expiry_date'].apply(format_exp_date)

                cnt_total = len(df_inv)
                cnt_near = (df_inv['FEFO Status'].str.startswith("⚠️")).sum()
                cnt_exp = (df_inv['FEFO Status'] == "⛔ Expired").sum()
                cnt_healthy = (df_inv['FEFO Status'] == "✅ Shelf-Life Healthy").sum()

                col_f1, col_f2, col_f3, col_f4 = st.columns(4)
                col_f1.metric("Total Tracked Batches", cnt_total)
                col_f2.metric("Near-Expiry Batches", int(cnt_near))
                col_f3.metric("Expired Batches", int(cnt_exp))
                col_f4.metric("Healthy Shelf-Life", int(cnt_healthy))

                col_srch, col_filt_fefo = st.columns([2, 1])
                with col_srch:
                    search_query = st.text_input("🔍 Search by Product Name or Batch No", "", key="inv_search")
                with col_filt_fefo:
                    fefo_filter = st.selectbox(
                        "FEFO Expiry Filter",
                        ["All Batches", "⚠️ Near-Expiry & Expired Only", "⚠️ Near-Expiry Only", "⛔ Expired Only"]
                    )

                if fefo_filter == "⚠️ Near-Expiry & Expired Only":
                    df_inv = df_inv[df_inv['FEFO Status'].str.startswith(("⚠️", "⛔"))]
                elif fefo_filter == "⚠️ Near-Expiry Only":
                    df_inv = df_inv[df_inv['FEFO Status'].str.startswith("⚠️")]
                elif fefo_filter == "⛔ Expired Only":
                    df_inv = df_inv[df_inv['FEFO Status'] == "⛔ Expired"]

                if search_query:
                    mask = (
                        df_inv['product_name'].astype(str).str.contains(search_query, case=False, na=False) |
                        df_inv['batch_no'].astype(str).str.contains(search_query, case=False, na=False)
                    )
                    df_inv = df_inv[mask]

                # Product Name in place of Item Code
                cols_to_display = ['product_name']
                if 'category' in df_inv.columns:
                    cols_to_display.append('category')
                cols_to_display.extend(['batch_no', 'Formatted Expiry', 'FEFO Status', 'qty_on_hand', 'qty_on_order', 'unit_cost'])
                cols_to_display = [c for c in cols_to_display if c in df_inv.columns]

                df_display = df_inv[cols_to_display].rename(columns={
                    'product_name': 'Product Name',
                    'category': 'Category',
                    'batch_no': 'Batch No',
                    'Formatted Expiry': 'Expiry Date',
                    'FEFO Status': 'FEFO Shelf-Life Status',
                    'qty_on_hand': 'On Hand',
                    'qty_on_order': 'On Order',
                    'unit_cost': 'Cost Price (₹)'
                })

                st.dataframe(df_display, use_container_width=True)
            else:
                st.info("No inventory batches in database yet. Please upload a MARG Excel file.")
        except Exception as e:
            st.error(f"Error fetching inventory: {e}")


# ---------------------------------------------------------------------------
# TAB 5: Audit & Compliance Trail
# ---------------------------------------------------------------------------
with tab_audit:
    st.subheader("5. Compliance & Decision Audit Trail")
    if is_healthy:
        try:
            aud_res = requests.get(f"{BACKEND_URL}/audit?limit=50", timeout=5)
            if aud_res.status_code == 200 and aud_res.json():
                df_aud = pd.DataFrame(aud_res.json())
                st.dataframe(
                    df_aud[['id', 'event_type', 'actor', 'entity_type', 'entity_id', 'created_at', 'details']],
                    use_container_width=True
                )
            else:
                st.info("No audit logs recorded yet.")
        except Exception as e:
            st.error(f"Error fetching audit trail: {e}")


# ---------------------------------------------------------------------------
# TAB 6: Live Application Rolling Logs
# ---------------------------------------------------------------------------
with tab_logs:
    st.subheader("6. Live Application Rolling Logs")
    st.caption("Inspect live streaming logs from the FastAPI backend and Streamlit UI services.")

    col_l1, col_l2 = st.columns([1, 4])
    with col_l1:
        log_source = st.radio("Log Source", ["FastAPI Backend (Port 8000)", "Streamlit UI (Port 8501)"])
        num_lines = st.slider("Lines to Tail", min_value=20, max_value=300, value=80, step=20)
        if st.button("🔄 Refresh Logs", use_container_width=True):
            st.rerun()

    with col_l2:
        if "FastAPI" in log_source:
            log_file = Path("/Users/debz/.gemini/antigravity-ide/brain/3aff40a2-008c-4545-841e-136020632ad6/.system_generated/tasks/task-72.log")
        else:
            log_file = Path("/Users/debz/.gemini/antigravity-ide/brain/3aff40a2-008c-4545-841e-136020632ad6/.system_generated/tasks/task-151.log")

        if log_file.exists():
            with log_file.open("r", encoding="utf-8", errors="replace") as f:
                content_lines = f.readlines()
            total_cnt = len(content_lines)
            st.info(f"📄 Showing last **{min(num_lines, total_cnt)}** of **{total_cnt}** lines from `{log_file.name}`")
            tail_lines = "".join(content_lines[-num_lines:])
            st.code(tail_lines, language="log")

            st.download_button(
                label=f"📥 Download Complete {log_source} Log",
                data="".join(content_lines),
                file_name=f"{log_source.lower().split()[0]}_rolling.log",
                mime="text/plain",
                use_container_width=True
            )
        else:
            st.warning(f"Log file not found at: `{log_file}`")
