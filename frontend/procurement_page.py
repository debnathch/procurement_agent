"""
Procurement Copilot UI Module.
Preserves 100% of the procurement recommendation, approval, FEFO inventory,
promo separation, and PO export functionality.
"""
import io
import time
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
    if not name:
        return True
    s_upper = str(name).strip().upper()
    if re.search(r'^\d+\s*ITEMS?$', s_upper) or s_upper in ('TOTAL', 'GRAND TOTAL', 'SUB TOTAL', 'SUMMARY'):
        return True
    return False

def render_procurement_page(BACKEND_URL: str, is_healthy: bool, health_info: dict):
    # Top Action Bar
    col_nav1, col_nav2 = st.columns([1, 5])
    with col_nav1:
        if st.button("🏠 Back to Dashboard", key="btn_procurement_back_top", use_container_width=True):
            st.session_state["current_page"] = "landing"
            st.rerun()
    with col_nav2:
        st.markdown('<div class="main-header">💊 MARG Procurement Copilot</div>', unsafe_allow_html=True)
        st.markdown('<div class="sub-header">Upload MARG ERP Excel, generate FEFO-aware procurement recommendations, and correct/approve suggested orders.</div>', unsafe_allow_html=True)

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
    
    if "upload_banner" in st.session_state and st.session_state["upload_banner"]:
        st.success(st.session_state.pop("upload_banner"))
    
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
    
    # Tab Configuration for persistent state & URL deep-linking
    TAB_CONFIG = [
        ("upload", "📂 Upload MARG Excel & Run"),
        ("proposals", "💡 Review & Correct Suggestions"),
        ("approved", "📦 Approved Orders / PO Export"),
        ("no_reorder", "🛡️ No Need for Reorder"),
        ("inventory", "📊 Inventory & FEFO Expiry"),
        ("promo_material", "🎁 Promo Material"),
        ("audit", "📜 Compliance Audit Log"),
        ("logs", "📋 Live Rolling Logs"),
    ]
    TAB_SLUGS = [s for s, _ in TAB_CONFIG]
    TAB_LABELS = [tab_label for _, tab_label in TAB_CONFIG]
    SLUG_TO_LABEL = dict(TAB_CONFIG)
    LABEL_TO_SLUG = {tab_label: s for s, tab_label in TAB_CONFIG}
    
    # Persistent tab tracking: reads from URL query params (browser refresh) or session state
    active_slug = st.query_params.get("tab")
    if active_slug and active_slug in SLUG_TO_LABEL:
        st.session_state["main_active_tab"] = SLUG_TO_LABEL[active_slug]
    elif "main_active_tab" not in st.session_state:
        st.session_state["main_active_tab"] = TAB_LABELS[0]
    
    def on_main_tab_changed():
        selected = st.session_state.get("main_active_tab")
        if selected in LABEL_TO_SLUG:
            st.query_params["tab"] = LABEL_TO_SLUG[selected]
    
    # Main Tabs with full state persistence across reruns and page refreshes
    tab_upload, tab_proposals, tab_approved, tab_no_reorder, tab_inventory, tab_promo, tab_audit, tab_logs = st.tabs(
        TAB_LABELS,
        key="main_active_tab",
        on_change=on_main_tab_changed
    )
    
    
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
                        st.session_state['uploader_version'] = st.session_state.get('uploader_version', 0) + 1
                        st.session_state.pop('last_upload_stats', None)
                        st.session_state.pop('last_upload_success_files', None)
                        st.session_state["upload_banner"] = "✅ Database purged completely! All historical data and uploaded documents cleared."
                        st.rerun()
                    else:
                        st.error(f"Error resetting database: {p_res.text}")
                except Exception as e:
                    st.error(f"Reset error: {e}")
    
        with col_up1:
            uploader_key = f"marg_file_uploader_{st.session_state.get('uploader_version', 0)}"
            uploaded_files = st.file_uploader(
                "Select MARG Export file(s) (.xlsx, .xls, or .csv) — You can select multiple files at once!",
                type=["xlsx", "xls", "csv"],
                accept_multiple_files=True,
                key=uploader_key,
                help="Upload raw MARG ERP exports directly without reformatting (Closing Stock, Manufacturer List, PCD Outstanding, etc.)"
            )
    
            # Show previous ingestion summary if available and no new files actively staged
            if not uploaded_files and "last_upload_stats" in st.session_state and st.session_state["last_upload_stats"]:
                stats = st.session_state["last_upload_stats"]
                n_files = st.session_state.get("last_upload_success_files", 1)
                st.success(f"✅ Ingested {n_files} file(s) into database:")
                col_s1, col_s2, col_s3, col_s4 = st.columns(4)
                col_s1.metric("Products Updated", stats.get('products_upserted', 0))
                col_s2.metric("Batches Recorded", stats.get('batches_inserted', 0))
                col_s3.metric("Suppliers Updated", stats.get('suppliers_upserted', 0))
                col_s4.metric("Sales Rows Imported", stats.get('sales_inserted', 0))
                st.info("👉 Switch to the **'Review & Correct Suggestions'** tab to review, adjust, and approve order suggestions!")
                if st.button("👉 Go to Review & Correct Suggestions", type="primary", key="nav_to_proposals_btn"):
                    st.session_state.pop("main_active_tab", None)
                    st.query_params["tab"] = "proposals"
                    st.rerun()
    
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
                                    else:
                                        st.error(f"Upload failed for {f.name}: {upload_res.text}")
                                except Exception as exc:
                                    st.error(f"Error processing {f.name}: {exc}")
    
                            if success_files > 0:
                                st.session_state['uploader_version'] = st.session_state.get('uploader_version', 0) + 1
                                st.session_state["last_upload_stats"] = cumulative_stats
                                st.session_state["last_upload_success_files"] = success_files
                                st.session_state["upload_banner"] = f"🎉 Ingestion & Analysis Complete! Processed {success_files} file(s). Top KPI metrics & order suggestions are now updated and synchronized with the DB."
                                st.rerun()
    
    
    # ---------------------------------------------------------------------------
    # TAB 2: Review & Correct Order Suggestions
    # ---------------------------------------------------------------------------
    with tab_proposals:
        st.subheader("2. Human-in-the-Loop: Review, Correct & Approve Suggestions")
        st.markdown("""
        The agent computes recommended replenishment quantities based on sales velocity, lead times, and safety stock.
        **🛡️ High-risk and near-expiry products are automatically excluded from reorder suggestions** (inspected in Tab 4: Inventory & FEFO Expiry).
        **You have full control to correct any value (quantity, supplier, notes) before approving.**
        """)
    
        col_ref, col_comp, col_mfr, col_filt, col_risk = st.columns([1, 2, 2, 1.5, 2])
        with col_ref:
            if st.button("🔄 Refresh Data", key="refresh_proposals_btn"):
                st.session_state.pop("main_active_tab", None)
                st.query_params["tab"] = "proposals"
                st.rerun()
    
        # Fetch proposals from backend
        raw_proposals = []
        if is_healthy:
            try:
                p_res = requests.get(f"{BACKEND_URL}/proposals", timeout=10)
                if p_res.status_code == 200:
                    raw_proposals = p_res.json()
            except Exception as e:
                st.error(f"Error loading proposals: {e}")
    
        # Filter out blacklisted non-medicine items
        raw_proposals = [p for p in raw_proposals if not is_excluded_product(p.get('product_name', ''))]
    
        # Exclude high and near-expiry risk products from Review and Correct suggestions tab
        raw_proposals = [
            p for p in raw_proposals
            if not (
                p.get('expiry_risk_score', 0) > 0 or
                p.get('near_expiry_qty', 0) > 0 or
                p.get('expiry_action') in ('PAUSE_PROCUREMENT', 'REDUCE_ORDER') or
                p.get('recommended_qty', 0) <= 0
            )
        ]
    
        # Compute company list from raw proposals
        available_companies = sorted(list({p.get('company') for p in raw_proposals if p.get('company') and p.get('company') != 'None'}))
        company_counts = {c: sum(1 for p in raw_proposals if p.get('company') == c) for c in available_companies}
        company_options = ["All Companies"] + available_companies
    
        # Compute manufacturer list from raw proposals
        available_mfrs = sorted(list({p.get('manufacturer') for p in raw_proposals if p.get('manufacturer') and p.get('manufacturer') not in ('None', 'General', '')}))
        mfr_counts = {m: sum(1 for p in raw_proposals if p.get('manufacturer') == m) for m in available_mfrs}
        mfr_options = ["All Manufacturers"] + available_mfrs
    
        with col_comp:
            selected_company = st.selectbox(
                "🏢 Filter by Company",
                options=company_options,
                format_func=lambda c: f"{c} ({company_counts.get(c, len(raw_proposals))} rows)" if c != "All Companies" else f"All Companies ({len(raw_proposals)} rows)",
                key="proposal_company_filter",
                help="Filter recommendations by pharmaceutical company"
            )
    
        with col_mfr:
            selected_mfr = st.selectbox(
                "🏭 Filter by Manufacturer",
                options=mfr_options,
                format_func=lambda m: f"{m} ({mfr_counts.get(m, len(raw_proposals))} rows)" if m != "All Manufacturers" else f"All Manufacturers ({len(raw_proposals)} rows)",
                key="proposal_mfr_filter",
                help="Filter recommendations by medicine manufacturer"
            )
    
        with col_filt:
            status_filter = st.selectbox("Filter Status", ["PENDING", "APPROVED_PENDING_EXECUTION", "EXECUTED", "REJECTED", "ALL"])
    
        with col_risk:
            qty_filter = st.selectbox(
                "Order Qty Flag",
                ["All Order Sizes", "🚩 High Qty (>50)", "🟡 Normal Qty (≤50)"]
            )
    
        # Apply Filters
        current_proposals = list(raw_proposals)
        if selected_company != "All Companies":
            current_proposals = [p for p in current_proposals if p.get('company') == selected_company]
    
        if selected_mfr != "All Manufacturers":
            current_proposals = [p for p in current_proposals if p.get('manufacturer') == selected_mfr]
    
        if status_filter != "ALL":
            current_proposals = [p for p in current_proposals if p.get('status') == status_filter]
    
        # Apply Order Quantity Flag Filter
        if qty_filter == "🚩 High Qty (>50)":
            current_proposals = [p for p in current_proposals if p.get('recommended_qty', 0) > 50]
        elif qty_filter == "🟡 Normal Qty (≤50)":
            current_proposals = [p for p in current_proposals if 0 < p.get('recommended_qty', 0) <= 50]
    
        # Sort alphabetically by product name (A-Z)
        current_proposals.sort(key=lambda p: str(p.get('product_name', '')).strip().upper())
    
        # Dynamic Search placeholder reflecting row count and selected company
        search_placeholder = f"Search within {len(current_proposals)} rows (product name or code)..."
        col_search, col_clear_search = st.columns([4, 1])
        with col_search:
            search_kw = st.text_input(
                "🔍 Search within proposals",
                value="",
                placeholder=search_placeholder,
                label_visibility="collapsed",
                key="proposal_search_kw"
            ).strip().lower()
        with col_clear_search:
            if st.button("🔄 Reset Search", use_container_width=True, key="proposal_reset_search_btn"):
                search_kw = ""
                st.session_state.pop("main_active_tab", None)
                st.query_params["tab"] = "proposals"
                st.rerun()
    
        if search_kw:
            current_proposals = [
                p for p in current_proposals
                if search_kw in p.get('product_name', '').lower() or search_kw in p.get('product_code', '').lower()
            ]
    
        # Showcase number of rows in the tab as well as active filter status
        st.info(
            f"📋 **Showing {len(current_proposals)} row(s)** in Review & Correct Suggestions "
            f"(Filtered by Company: **{selected_company}** | Manufacturer: **{selected_mfr}** | Status: **{status_filter}** | Order Qty: **{qty_filter}** | Out of {len(raw_proposals)} total)"
        )
    
        if not current_proposals:
            if selected_company != "All Companies" or selected_mfr != "All Manufacturers" or search_kw:
                st.warning(
                    f"No proposal rows found matching company '{selected_company}' and manufacturer '{selected_mfr}'"
                    + (f" with keyword '{search_kw}'" if search_kw else "")
                    + " (0 rows displayed). Select 'All Companies' / 'All Manufacturers' or clear search to view more rows."
                )
            else:
                st.info("No proposals found for the selected filter. Upload a MARG Excel or click below to trigger a run:")
                if st.button("⚡ Run Procurement Agent Now", key="trigger_run_btn"):
                    if is_healthy:
                        r_res = requests.post(f"{BACKEND_URL}/runs", json={"lead_time_days": int(config_lead_time)}, timeout=30)
                        if r_res.status_code == 201:
                            st.success("Procurement Run finished!")
                            st.session_state.pop("main_active_tab", None)
                            st.query_params["tab"] = "proposals"
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
                        'Order Qty Flag': '🚩 > 50 (High Qty)' if p['recommended_qty'] > 50 else '🟡 ≤ 50 (Normal Qty)',
                        'Company': p.get('company', 'General'),
                        'Manufacturer': p.get('manufacturer', 'General'),
                        'Default Supplier': p.get('supplier_name', 'Default Supplier'),
                        'Batch(es)': p.get('batch_numbers', 'DEFAULT'),
                        'Recommended Qty': p['recommended_qty'],
                        'Last Purchase Cost (₹)': p['unit_cost'],
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
    
                # Order quantity color flag
                qty_flag_icon = "🚩" if p['recommended_qty'] > 50 else "🟡"
                qty_flag_text = "🚩 High Qty (>50)" if p['recommended_qty'] > 50 else "🟡 Normal Qty (≤50)"
    
                with st.expander(
                    f"{qty_flag_icon} **{p['product_name']}** (`{p['product_code']}`) | 🏢 **{p.get('company', 'General')}** | 🏭 **{p.get('manufacturer', 'General')}** | 🚚 Supplier: **{p.get('supplier_name', 'Default Supplier')}** | 🏷️ Batch(es): `{p.get('batch_numbers', 'DEFAULT')}` — Suggested: **{p['recommended_qty']:g} units** [{qty_flag_text}] (₹{p['estimated_value']:,.2f}) | Status: :{st_color}[{p['status']}]",
                    expanded=(p['status'] == 'PENDING' and len(page_proposals) <= 5)
                ):
                    if p['recommended_qty'] > 50:
                        st.error(f"🚩 **Order Quantity Red Flag (> 50 Units)**: Recommended order quantity is **{p['recommended_qty']:g} units** (> 50 units). Default supplier: **{p.get('supplier_name', 'Default Supplier')}**, Last purchase cost: **₹{p.get('unit_cost', 0):.2f}**.")
                    else:
                        st.warning(f"🟡 **Order Quantity Yellow Flag (≤ 50 Units)**: Recommended order quantity is **{p['recommended_qty']:g} units** (≤ 50 units). Default supplier: **{p.get('supplier_name', 'Default Supplier')}**, Last purchase cost: **₹{p.get('unit_cost', 0):.2f}**.")
    
                    st.markdown(risk_badge, unsafe_allow_html=True)
                    st.markdown(f"**Agent Rationale:** {p.get('rationale', 'N/A')}")
    
                    # Metrics row
                    c1, c2, c3, c4, c5 = st.columns(5)
                    c1.metric("Stock on Hand", f"{p.get('stock_on_hand', 0):g}")
                    c2.metric("Stock on Order", f"{p.get('stock_on_order', 0):g}")
                    c3.metric("Near-Expiry Stock", f"{p.get('near_expiry_qty', 0):g}")
                    c4.metric("Avg Daily Demand", f"{p.get('avg_daily_demand', 0):.2f}/day")
                    c5.metric("Lead Time", f"{p.get('lead_time_days', int(config_lead_time))} days")
    
                    csup1, csup2 = st.columns(2)
                    csup1.info(f"🚚 **Default Supplier (Last Purchased):** {p.get('supplier_name', 'Default Supplier')}")
                    csup2.info(f"💰 **Last Purchase Cost:** ₹{p.get('unit_cost', 0):.2f} per unit")
    
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
                                    try:
                                        decide_res = requests.post(
                                            f"{BACKEND_URL}/proposals/{p_id}/decide",
                                            json={
                                                "action": "approve",
                                                "approved_qty": corrected_qty,
                                                "supplier_id": corrected_supplier_id,
                                                "reason": human_note,
                                                "actor": "human-reviewer"
                                            },
                                            timeout=15
                                        )
                                        if decide_res.status_code == 200:
                                            st.toast(f"✅ Proposal for {p['product_name']} successfully approved!", icon="🎉")
                                            st.session_state["main_active_tab"] = SLUG_TO_LABEL.get("proposals", TAB_LABELS[1])
                                            st.query_params["tab"] = "proposals"
                                            time.sleep(0.3)
                                            st.rerun()
                                        else:
                                            try:
                                                err_msg = decide_res.json().get('detail', decide_res.text)
                                            except Exception:
                                                err_msg = decide_res.text
                                            st.error(f"⚠️ Approval blocked by guardrail: {err_msg}")
                                            st.toast(f"Approval blocked: {err_msg}", icon="⚠️")
                                    except Exception as exc:
                                        st.error(f"❌ Failed to communicate with backend at {BACKEND_URL}: {exc}")
                                        st.toast(f"Connection error: {exc}", icon="❌")
    
                        with col_btn2:
                            reject_note = st.text_input("Rejection Reason", "Not needed this cycle", key=f"rej_note_{p_id}")
                            if st.button("❌ Reject Proposal", key=f"btn_rej_{p_id}", use_container_width=True):
                                with st.spinner("Rejecting proposal..."):
                                    try:
                                        decide_res = requests.post(
                                            f"{BACKEND_URL}/proposals/{p_id}/decide",
                                            json={
                                                "action": "reject",
                                                "reason": reject_note,
                                                "actor": "human-reviewer"
                                            },
                                            timeout=15
                                        )
                                        if decide_res.status_code == 200:
                                            st.toast(f"Proposal {p_id} rejected.", icon="🗑️")
                                            st.session_state["main_active_tab"] = SLUG_TO_LABEL.get("proposals", TAB_LABELS[1])
                                            st.query_params["tab"] = "proposals"
                                            time.sleep(0.3)
                                            st.rerun()
                                        else:
                                            st.error(f"Rejection error: {decide_res.text}")
                                    except Exception as exc:
                                        st.error(f"❌ Failed to communicate with backend: {exc}")
    
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
    
            st.markdown("---")
            st.markdown("#### ↩️ Remove Items from Approved Orders")
            st.caption(
                "Removing an item deletes it from this finalized approved orders list and **automatically returns it back to the 'Review & Correct Suggestions' tab** for quantity adjustments or re-approval."
            )
    
            col_rem1, col_rem2 = st.columns([3, 1])
            with col_rem1:
                item_options = {
                    p['id']: f"[{p['product_code']}] {p['product_name']} — Approved: {p.get('approved_qty', 0):g} units (₹{((p.get('approved_qty') or 0) * (p.get('unit_cost') or 0)):,.2f}) | Supplier: {p.get('supplier_name', 'N/A')}"
                    for p in approved_proposals
                }
                selected_to_remove = st.multiselect(
                    "Select Approved Item(s) to Remove:",
                    options=list(item_options.keys()),
                    format_func=lambda x: item_options.get(x, str(x)),
                    placeholder="Choose one or more items to remove and return to review...",
                    key="select_approved_to_remove"
                )
            with col_rem2:
                st.write("")
                st.write("")
                remove_btn_label = f"🗑️ Remove Selected ({len(selected_to_remove)})" if selected_to_remove else "🗑️ Remove Item"
                if st.button(remove_btn_label, type="secondary", disabled=(len(selected_to_remove) == 0), use_container_width=True, key="btn_remove_approved_items"):
                    with st.spinner("Returning items to Review & Correct Suggestions..."):
                        success_count = 0
                        for pid in selected_to_remove:
                            try:
                                rev_res = requests.post(
                                    f"{BACKEND_URL}/proposals/{pid}/unapprove",
                                    json={"action": "unapprove", "reason": "Removed from approved list by reviewer", "actor": "human-reviewer"},
                                    timeout=10
                                )
                                if rev_res.status_code == 200:
                                    success_count += 1
                            except Exception:
                                pass
                        if success_count > 0:
                            st.success(f"✅ Successfully removed {success_count} item(s) from Approved Orders! They are now back in the 'Review & Correct Suggestions' tab.")
                            st.rerun()
                        else:
                            st.error("Failed to remove selected items.")
    
            # Expandable list of approved items with individual quick-removal buttons
            with st.expander("📋 Individual Item Quick-Removal List (Click to expand)", expanded=False):
                st.markdown("Remove any individual product from the approved list with one click:")
                for p in approved_proposals:
                    row_c1, row_c2, row_c3 = st.columns([4, 2, 2])
                    with row_c1:
                        st.write(f"**{p['product_name']}** (`{p['product_code']}`)")
                        st.caption(f"Supplier: {p.get('supplier_name', 'N/A')} | Batch: `{p.get('batch_numbers', 'DEFAULT')}`")
                    with row_c2:
                        val = (p.get('approved_qty') or 0) * (p.get('unit_cost') or 0)
                        st.write(f"Approved: **{p.get('approved_qty', 0):g} units**")
                        st.caption(f"Total: ₹{val:,.2f}")
                    with row_c3:
                        if st.button("🗑️ Remove Item", key=f"quick_remove_p_{p['id']}", use_container_width=True, help="Remove this item and return it to Review & Correct Suggestions"):
                            with st.spinner(f"Removing {p['product_name']}..."):
                                try:
                                    rev_res = requests.post(
                                        f"{BACKEND_URL}/proposals/{p['id']}/unapprove",
                                        json={"action": "unapprove", "reason": "Removed from approved list by reviewer", "actor": "human-reviewer"},
                                        timeout=10
                                    )
                                    if rev_res.status_code == 200:
                                        st.success(f"✅ '{p['product_name']}' removed from Approved Orders and returned to Review & Correct Suggestions!")
                                        st.rerun()
                                    else:
                                        st.error(f"Error removing item: {rev_res.text}")
                                except Exception as e:
                                    st.error(f"Error: {e}")
                    st.divider()
    
    
    # ---------------------------------------------------------------------------
    # TAB: No Need for Reorder (Net Need < 0)
    # ---------------------------------------------------------------------------
    with tab_no_reorder:
        st.subheader("🛡️ Products with No Need for Reorder (Net Need < 0)")
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
    
        # Compute company list from no-reorder items
        nr_companies = sorted(list({p.get('company') for p in no_reorder_items if p.get('company') and p.get('company') != 'None'}))
        nr_company_counts = {c: sum(1 for p in no_reorder_items if p.get('company') == c) for c in nr_companies}
        nr_company_options = ["All Companies"] + nr_companies
    
        # Compute manufacturer list from no-reorder items
        nr_mfrs = sorted(list({p.get('manufacturer') for p in no_reorder_items if p.get('manufacturer') and p.get('manufacturer') not in ('None', 'General', '')}))
        nr_mfr_counts = {m: sum(1 for p in no_reorder_items if p.get('manufacturer') == m) for m in nr_mfrs}
        nr_mfr_options = ["All Manufacturers"] + nr_mfrs
    
        # Top Metrics Bar
        if total_no_reorder_count > 0:
            total_healthy_val = sum(
                p.get('healthy_stock_value', p.get('inventory_value',
                    p.get('available_stock', p.get('usable_before_expiry', 0.0)) * p.get('unit_cost', 0.0)
                ))
                for p in no_reorder_items
            )
            avg_cov_days = sum(min(p.get('coverage_days', 0.0), 365.0) for p in no_reorder_items) / total_no_reorder_count
            zero_risk_count = sum(1 for p in no_reorder_items if p.get('expiry_risk_score', 0.0) == 0.0)
    
            col_nr1, col_nr2, col_nr3, col_nr4 = st.columns(4)
            with col_nr1:
                st.metric("Total Products Not Needing Reorder", total_no_reorder_count)
            with col_nr2:
                st.metric("Total Healthy Stock Valuation", f"₹{total_healthy_val:,.2f}")
            with col_nr3:
                st.metric("Avg Inventory Coverage", f"{avg_cov_days:.1f} days")
            with col_nr4:
                st.metric("100% FEFO Healthy", f"{zero_risk_count / total_no_reorder_count:.0%}")
    
            st.success(
                f"💰 **Total Healthy Stock Valuation:** **₹{total_healthy_val:,.2f}** "
                f"(Computed across all {total_no_reorder_count} covered products by multiplying available healthy stock with purchase cost for each individual product and summing the total)."
            )
    
        st.markdown("---")
    
        # Search Bar with Company & Manufacturer Filters, dedicated Search and Reset Buttons
        st.markdown("##### 🔍 Search & Filter Products")
        col_nr_comp, col_nr_mfr, col_s1, col_s2, col_s3 = st.columns([1.5, 1.5, 2.5, 1, 1])
    
        with col_nr_comp:
            selected_nr_company = st.selectbox(
                "🏢 Filter by Company",
                options=nr_company_options,
                format_func=lambda c: f"{c} ({nr_company_counts.get(c, total_no_reorder_count)} rows)" if c != "All Companies" else f"All Companies ({total_no_reorder_count} rows)",
                key="no_reorder_company_filter",
                help="Filter surplus stock by pharmaceutical company"
            )
    
        with col_nr_mfr:
            selected_nr_mfr = st.selectbox(
                "🏭 Filter by Manufacturer",
                options=nr_mfr_options,
                format_func=lambda m: f"{m} ({nr_mfr_counts.get(m, total_no_reorder_count)} rows)" if m != "All Manufacturers" else f"All Manufacturers ({total_no_reorder_count} rows)",
                key="no_reorder_mfr_filter",
                help="Filter surplus stock by medicine manufacturer"
            )
    
        # Active filters
        display_items = list(no_reorder_items)
        if selected_nr_company != "All Companies":
            display_items = [p for p in display_items if p.get('company') == selected_nr_company]
    
        if selected_nr_mfr != "All Manufacturers":
            display_items = [p for p in display_items if p.get('manufacturer') == selected_nr_mfr]
    
        with col_s1:
            nr_search_placeholder = f"Search within {len(display_items)} rows (e.g. medicine name, code)..."
            search_kw = st.text_input(
                "Search product by name or item code",
                value="",
                placeholder=nr_search_placeholder,
                label_visibility="collapsed",
                key="input_search_no_reorder"
            ).strip().lower()
    
        with col_s2:
            st.button("🔍 Search Product", type="primary", use_container_width=True, key="btn_do_search_no_reorder")
    
        with col_s3:
            if st.button("🔄 Show All Products", use_container_width=True, key="btn_clear_search_no_reorder"):
                search_kw = ""
                st.session_state.pop("main_active_tab", None)
                st.query_params["tab"] = "no_reorder"
                st.rerun()
    
        # Active search filter
        if search_kw:
            display_items = [
                p for p in display_items
                if search_kw in p.get('product_name', '').lower() or search_kw in p.get('product_code', '').lower()
            ]
    
        # Ensure ordered by character (alphabetical order A-Z by product_name)
        display_items.sort(key=lambda p: str(p.get('product_name', '')).strip().upper())
    
        # Showcase number of rows in tab as well as placeholder
        if selected_nr_company != "All Companies" or selected_nr_mfr != "All Manufacturers" or search_kw:
            st.info(
                f"📋 **Showing {len(display_items)} row(s)** with Net Need < 0 "
                f"(Filtered by Company: **{selected_nr_company}** | Manufacturer: **{selected_nr_mfr}**"
                + (f" | Keyword: `'{search_kw}'`" if search_kw else "")
                + f" | Out of **{total_no_reorder_count}** total products)."
            )
        else:
            st.info(f"📋 **Total List Count**: Showing all **{total_no_reorder_count}** products with Net Need < 0 in alphabetical order (A–Z). No reorder needed.")
    
        if not display_items:
            if selected_nr_company != "All Companies" or selected_nr_mfr != "All Manufacturers" or search_kw:
                st.warning(f"No products found for company '{selected_nr_company}' and manufacturer '{selected_nr_mfr}'" + (f" matching '{search_kw}'" if search_kw else "") + " (0 rows displayed). Try different filters or click 'Show All Products'.")
            else:
                st.info("No products currently have Net Need < 0. Run the procurement agent after uploading stock and sales data.")
        else:
            # Direct summary table view (always visible)
            df_no_reorder = pd.DataFrame([
                {
                    'Product Name': p['product_name'],
                    'Product Code': p['product_code'],
                    'Company': p.get('company', 'General'),
                    'Manufacturer': p.get('manufacturer', 'General'),
                    'Default Supplier': p.get('supplier_name', 'Default Supplier'),
                    'Batch(es)': p.get('batch_numbers', 'DEFAULT'),
                    'Stock on Hand': p['stock_on_hand'],
                    'Available Healthy Stock': p.get('available_stock', p.get('usable_before_expiry', 0.0)),
                    'Purchase Cost (₹)': p['unit_cost'],
                    'Healthy Stock Value (₹)': round(p.get('available_stock', p.get('usable_before_expiry', 0.0)) * p['unit_cost'], 2),
                    'On Order': p['stock_on_order'],
                    'Daily Demand': p['avg_daily_demand'],
                    'Target Required': p['target_stock'],
                    'Net Need': f"{p['net_need']:.2f}",
                    'Surplus Units': f"+{p['surplus_qty']:.2f}",
                    'Coverage (Days)': f"{p['coverage_days']:.1f}d" if p['coverage_days'] < 999 else "No Demand",
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
                avail_stock = p.get('available_stock', p.get('usable_before_expiry', 0.0))
                healthy_val = round(avail_stock * p.get('unit_cost', 0.0), 2)
    
                with st.expander(
                    f"**{p['product_name']}** (`{p['product_code']}`) | 🏢 **{p.get('company', 'General')}** | 🏭 **{p.get('manufacturer', 'General')}** | 🚚 Supplier: **{p.get('supplier_name', 'Default Supplier')}** | 🏷️ Batch(es): `{p.get('batch_numbers', 'DEFAULT')}` — Healthy Stock: **{avail_stock:g} units** (₹{healthy_val:,.2f}) | Net Need: **{p['net_need']:g}**",
                    expanded=False
                ):
                    st.markdown(f"**Agent Rationale:** {p['rationale']}")
                    c1, c2, c3, c4, c5 = st.columns(5)
                    c1.metric("Stock on Hand", f"{p['stock_on_hand']:g}")
                    c2.metric("Available Healthy Stock", f"{avail_stock:g}")
                    c3.metric("Stock on Order", f"{p['stock_on_order']:g}")
                    c4.metric("Avg Daily Demand", f"{p['avg_daily_demand']:.2f}/day")
                    c5.metric("Target Stock Needed", f"{p['target_stock']:g}")
    
                    c6, c7, c8, c9 = st.columns(4)
                    c6.metric("Net Need", f"{p['net_need']:g} units", delta=f"{p['net_need']:g} (No Reorder)")
                    c7.metric("Surplus Stock", f"+{p['surplus_qty']:g} units")
                    c8.metric("Days Coverage", f"{p['coverage_days']:.1f}d" if p['coverage_days'] < 999 else "No Demand")
                    c9.metric("Healthy Stock Capital", f"₹{healthy_val:,.2f}", help="Available usable stock × Purchase cost")
    
    
    # ---------------------------------------------------------------------------
    # TAB 4: Current Inventory & FEFO Expiry
    # ---------------------------------------------------------------------------
    with tab_inventory:
        st.subheader("4. Inventory Batches & FEFO Expiry Status")
        if is_healthy:
            try:
                inv_res = requests.get(f"{BACKEND_URL}/inventory?exclude_healthy=true", timeout=10)
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
                    horizon_days = int(getattr(settings, 'expiry_risk_horizon_days', 180)) if settings else 180
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
    
                    if 'fefo_status' in df_inv.columns:
                        df_inv['FEFO Status'] = df_inv['fefo_status']
                    else:
                        df_inv['FEFO Status'] = df_inv['expiry_date'].apply(calc_batch_fefo)
    
                    # Strictly isolate at-risk stock: exclude any Shelf-Life Healthy items or non-expiry entries from this tab
                    df_inv = df_inv[~df_inv['FEFO Status'].str.startswith("✅")]
                    df_inv = df_inv[~df_inv['FEFO Status'].isin(["Unknown / General", "Unknown"])]
    
                    # Mandatory Business Rule: Products in healthy state in "No Need for Reorder" tab
                    # MUST NOT show in the "Inventory and FEFO" tab
                    if 'no_reorder_items' not in locals() or not no_reorder_items:
                        try:
                            nr_fetch = requests.get(f"{BACKEND_URL}/procurement/no-reorder", timeout=10)
                            if nr_fetch.status_code == 200:
                                no_reorder_items = nr_fetch.json()
                        except Exception:
                            no_reorder_items = []
    
                    if no_reorder_items:
                        healthy_nr_codes = {
                            p.get('product_code') for p in no_reorder_items
                            if p.get('expiry_action') != 'PAUSE_PROCUREMENT'
                            and p.get('near_expiry_qty', 0) <= 0
                            and p.get('expired_qty', 0) <= 0
                        }
                        healthy_nr_names = {
                            str(p.get('product_name', '')).strip().upper() for p in no_reorder_items
                            if p.get('expiry_action') != 'PAUSE_PROCUREMENT'
                            and p.get('near_expiry_qty', 0) <= 0
                            and p.get('expired_qty', 0) <= 0
                        }
                        df_inv = df_inv[
                            ~df_inv['product_code'].isin(healthy_nr_codes) &
                            ~df_inv['product_name'].astype(str).str.strip().str.upper().isin(healthy_nr_names)
                        ]
    
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
    
                    # Placeholder for selected rows count (updates after filters are applied)
                    metric_selected_placeholder = st.empty()
    
                    # Company & Manufacturer filters
                    inv_companies = sorted(list({c for c in df_inv['company'].dropna().unique() if c and c not in ('None', '')}))
                    inv_company_options = ["All Companies"] + inv_companies
    
                    inv_mfrs = sorted(list({m for m in df_inv['manufacturer'].dropna().unique() if m and m not in ('None', 'General', '')}))
                    inv_mfr_options = ["All Manufacturers"] + inv_mfrs
    
                    col_icomp, col_imfr, col_filt_fefo, col_srch = st.columns([1.5, 1.5, 1.5, 2])
                    with col_icomp:
                        sel_inv_company = st.selectbox("🏢 Filter by Company", inv_company_options, key="inv_company_filter")
                    with col_imfr:
                        sel_inv_mfr = st.selectbox("🏭 Filter by Manufacturer", inv_mfr_options, key="inv_mfr_filter")
                    with col_filt_fefo:
                        fefo_filter = st.selectbox(
                            "FEFO Expiry Filter",
                            ["⚠️ Near-Expiry & Expired (All At-Risk)", "⚠️ Near-Expiry Only (≤ 180d)", "⛔ Expired Only"],
                            key="inv_fefo_filter"
                        )
                    with col_srch:
                        search_query = st.text_input("🔍 Search Product Name or Batch", "", key="inv_search")
    
                    if sel_inv_company != "All Companies":
                        df_inv = df_inv[df_inv['company'] == sel_inv_company]
                    if sel_inv_mfr != "All Manufacturers":
                        df_inv = df_inv[df_inv['manufacturer'] == sel_inv_mfr]
                    if fefo_filter == "⚠️ Near-Expiry & Expired (All At-Risk)":
                        df_inv = df_inv[df_inv['FEFO Status'].str.startswith(("⚠️", "⛔"))]
                    elif fefo_filter == "⚠️ Near-Expiry Only (≤ 180d)":
                        df_inv = df_inv[df_inv['FEFO Status'].str.startswith("⚠️")]
                    elif fefo_filter == "⛔ Expired Only":
                        df_inv = df_inv[df_inv['FEFO Status'] == "⛔ Expired"]
    
                    if search_query:
                        mask = (
                            df_inv['product_name'].astype(str).str.contains(search_query, case=False, na=False) |
                            df_inv['product_code'].astype(str).str.contains(search_query, case=False, na=False) |
                            df_inv['batch_no'].astype(str).str.contains(search_query, case=False, na=False)
                        )
                        df_inv = df_inv[mask]
    
                    # Compute total quantity and total cost amount for the filtered list
                    total_filtered_qty = float(df_inv['qty_on_hand'].sum()) if 'qty_on_hand' in df_inv.columns else 0.0
                    total_filtered_amt = (
                        float(df_inv['inventory_value'].sum())
                        if 'inventory_value' in df_inv.columns
                        else float((df_inv['qty_on_hand'] * df_inv.get('unit_cost', 0.0)).sum())
                    )
    
                    # Update the placeholder with selected rows count and total amount after filtering
                    with metric_selected_placeholder.container():
                        col_m1, col_m2, col_m3 = st.columns(3)
                        with col_m1:
                            st.metric("selected rows : ", len(df_inv))
                        with col_m2:
                            st.metric("total stock qty : ", f"{total_filtered_qty:g} units")
                        with col_m3:
                            st.metric("total amount : ", f"₹{total_filtered_amt:,.2f}")
    
                    # Format DataFrame for display
                    cols_to_display = ['product_name', 'product_code', 'company', 'manufacturer', 'batch_no', 'Formatted Expiry', 'FEFO Status', 'qty_on_hand', 'qty_on_order', 'unit_cost', 'inventory_value']
                    cols_to_display = [c for c in cols_to_display if c in df_inv.columns]
    
                    df_display = df_inv[cols_to_display].rename(columns={
                        'product_name': 'Product Name',
                        'product_code': 'Product Code',
                        'company': 'Company',
                        'manufacturer': 'Manufacturer',
                        'batch_no': 'Batch(es)',
                        'Formatted Expiry': 'Earliest Expiry',
                        'FEFO Status': 'FEFO Shelf-Life Status',
                        'qty_on_hand': 'Current Stock',
                        'qty_on_order': 'On Order',
                        'unit_cost': 'Cost Price (₹)',
                        'inventory_value': 'Total Value (₹)'
                    })
    
                    st.dataframe(df_display, use_container_width=True, hide_index=True)
    
                    if len(df_inv) > 0:
                        st.info(
                            f"💰 **Total Amount for Selected List:** **₹{total_filtered_amt:,.2f}** "
                            f"across **{len(df_inv)}** selected rows (Physical Stock: **{total_filtered_qty:g}** units)."
                        )
    
                    col_inv_exp, _ = st.columns([1, 4])
                    with col_inv_exp:
                        csv_inv = df_display.to_csv(index=False).encode('utf-8')
                        st.download_button(
                            label="📥 Download Inventory CSV",
                            data=csv_inv,
                            file_name="inventory_stock.csv",
                            mime="text/csv",
                            use_container_width=True
                        )
                else:
                    st.info("No inventory batches in database yet. Please upload a MARG Excel file.")
            except Exception as e:
                st.error(f"Error fetching inventory: {e}")
    
    
    # ---------------------------------------------------------------------------
    # TAB: Promo Material (Excluded from Medical Proposals & Inventory)
    # ---------------------------------------------------------------------------
    with tab_promo:
        st.subheader("🎁 Promotional Materials & Stationery")
        st.markdown("""
        Items where **both Manufacturer and Supplier were blank** in the uploaded stock Excel (e.g. promotional gifts, diaries, pens, bags, banners).
        These items are **strictly isolated** from pharmaceutical inventory, replenishment forecasting, and procurement proposals.
        """)
    
        promo_items = []
        if is_healthy:
            try:
                pr_res = requests.get(f"{BACKEND_URL}/promo-material", timeout=10)
                if pr_res.status_code == 200:
                    promo_items = pr_res.json()
            except Exception as e:
                st.error(f"Error fetching promo materials: {e}")
    
        if not promo_items:
            st.info("No promotional material items detected in the current stock data.")
        else:
            total_promo_count = len(promo_items)
            total_promo_qty = sum(p.get('qty_on_hand', 0) for p in promo_items)
            total_promo_val = sum(p.get('total_value', 0) for p in promo_items)
    
            col_pr1, col_pr2, col_pr3 = st.columns(3)
            col_pr1.metric("Total Promo Items", total_promo_count)
            col_pr2.metric("Total Physical Stock", f"{total_promo_qty:g} units")
            col_pr3.metric("Total Stock Capital", f"₹{total_promo_val:,.2f}")
    
            st.markdown("---")
    
            # Filters
            promo_companies = sorted(list({p.get('company') for p in promo_items if p.get('company') and p.get('company') != 'None'}))
            promo_company_options = ["All Companies"] + promo_companies
    
            col_pcomp, col_psrch = st.columns([1.5, 3])
            with col_pcomp:
                sel_promo_company = st.selectbox("🏢 Filter by Company", promo_company_options, key="promo_company_filter")
            with col_psrch:
                promo_search = st.text_input("🔍 Search Promo Material by Name or Item Code", "", key="promo_search").strip().lower()
    
            filtered_promo = list(promo_items)
            if sel_promo_company != "All Companies":
                filtered_promo = [p for p in filtered_promo if p.get('company') == sel_promo_company]
            if promo_search:
                filtered_promo = [
                    p for p in filtered_promo
                    if promo_search in p.get('product_name', '').lower() or promo_search in p.get('product_code', '').lower()
                ]
    
            filtered_promo.sort(key=lambda p: str(p.get('product_name', '')).strip().upper())
    
            st.info(f"Showing **{len(filtered_promo)}** of **{total_promo_count}** promo items.")
    
            if filtered_promo:
                df_promo = pd.DataFrame([
                    {
                        'Product Name': p['product_name'],
                        'Product Code': p['product_code'],
                        'Company': p.get('company', 'General'),
                        'Manufacturer': p.get('manufacturer', 'None (Promo)'),
                        'Supplier': p.get('supplier_name', 'None (Promo)'),
                        'Batch(es)': p.get('batch_no', 'DEFAULT'),
                        'Current Stock': p['qty_on_hand'],
                        'Cost Price (₹)': p['unit_cost'],
                        'Total Value (₹)': p['total_value'],
                        'Expiry Date': p.get('expiry_date', 'N/A'),
                        'Unit': p.get('unit', 'pcs'),
                    }
                    for p in filtered_promo
                ])
    
                st.dataframe(df_promo, use_container_width=True, hide_index=True)
    
                col_pr_exp, _ = st.columns([1, 4])
                with col_pr_exp:
                    csv_promo = df_promo.to_csv(index=False).encode('utf-8')
                    st.download_button(
                        label="📥 Download Promo Material CSV",
                        data=csv_promo,
                        file_name="promo_material_items.csv",
                        mime="text/csv",
                        use_container_width=True
                    )
    
    
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
    
            try:
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
                    st.info("Log file is not configured for this environment.")
            except Exception as e:
                st.info(f"Logs are currently being written to console/stdout.")
