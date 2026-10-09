"""
Executive Landing Page for MARG Pharmaceutical Intelligence Suite.

Provides unified entry point to:
1. Procurement Intelligence Copilot
2. Sales Intelligence & Customer Recommendations
"""
import requests
import streamlit as st


def render_landing_page(BACKEND_URL: str, is_healthy: bool):
    # Hero Section
    st.markdown("""
    <div style="background: linear-gradient(135deg, #1E293B 0%, #0F172A 100%); padding: 32px 36px; border-radius: 16px; margin-bottom: 28px; color: white; box-shadow: 0 10px 25px -5px rgba(0,0,0,0.2);">
        <div style="display: flex; align-items: center; justify-content: space-between;">
            <div>
                <span style="background: #3B82F6; color: white; padding: 4px 12px; border-radius: 9999px; font-size: 0.8rem; font-weight: 600; text-transform: uppercase; letter-spacing: 0.05em;">
                    Local-First Pharmaceutical ERP Suite
                </span>
                <h1 style="color: white; margin: 12px 0 8px 0; font-size: 2.3rem; font-weight: 800; letter-spacing: -0.02em;">
                    🏥 MARG Pharma Enterprise Hub
                </h1>
                <p style="color: #94A3B8; font-size: 1.05rem; margin: 0; max-width: 650px;">
                    Intelligent distribution platform integrating inventory FEFO optimization, automated procurement reorder forecasting, and customer sales recommendations.
                </p>
            </div>
        </div>
    </div>
    """, unsafe_allow_html=True)

    # Fetch live overview stats if backend is alive
    stats_procurement = {"pending_count": 0, "approved_count": 0, "total_value": 0.0}
    stats_sales = {"customer_count": 0, "total_dues": 0.0, "total_sales": 0.0}

    if is_healthy:
        try:
            p_res = requests.get(f"{BACKEND_URL}/proposals", timeout=4)
            if p_res.status_code == 200:
                props = p_res.json()
                pending = [p for p in props if p.get("status") == "PENDING"]
                approved = [p for p in props if p.get("status") in ("APPROVED_PENDING_EXECUTION", "EXECUTED")]
                stats_procurement["pending_count"] = len(pending)
                stats_procurement["approved_count"] = len(approved)
                stats_procurement["total_value"] = sum(p.get("estimated_value", 0) for p in pending)
        except Exception:
            pass

        try:
            c_res = requests.get(f"{BACKEND_URL}/sales/customers", timeout=4)
            if c_res.status_code == 200:
                custs = c_res.json()
                stats_sales["customer_count"] = len(custs)
                stats_sales["total_dues"] = sum(c.get("current_dues", 0) for c in custs)
                stats_sales["total_sales"] = sum(c.get("total_sales", 0) for c in custs)
        except Exception:
            pass

    # Quick KPI Summary
    col_kpi1, col_kpi2, col_kpi3, col_kpi4 = st.columns(4)
    with col_kpi1:
        st.metric("Pending PO Proposals", f"{stats_procurement['pending_count']} items")
    with col_kpi2:
        st.metric("Pending Purchase Value", f"₹{stats_procurement['total_value']:,.2f}")
    with col_kpi3:
        st.metric("Active MARG Customers", f"{stats_sales['customer_count']} parties")
    with col_kpi4:
        st.metric("Total Outstanding Dues", f"₹{stats_sales['total_dues']:,.2f}")

    st.markdown("<div style='height: 16px;'></div>", unsafe_allow_html=True)

    # 2 Launch Cards
    col_card1, col_card2 = st.columns(2, gap="large")

    with col_card1:
        st.markdown("""
        <div style="background-color: #F8FAFC; border: 2px solid #E2E8F0; border-radius: 14px; padding: 24px; min-height: 340px; display: flex; flex-direction: column; justify-content: space-between;">
            <div>
                <div style="font-size: 2.2rem; margin-bottom: 8px;">💊</div>
                <h3 style="color: #0F172A; margin: 0 0 8px 0; font-size: 1.35rem; font-weight: 700;">Procurement Copilot</h3>
                <p style="color: #475569; font-size: 0.95rem; line-height: 1.5; margin-bottom: 16px;">
                    Automated FEFO replenishment intelligence for warehouse inventory.
                </p>
                <ul style="color: #64748B; font-size: 0.88rem; padding-left: 20px; line-height: 1.6; margin-bottom: 20px;">
                    <li><strong>FEFO Expiry Horizon</strong>: Flag near-expiry batches (≤180 days)</li>
                    <li><strong>Safety Stock & Lead Times</strong>: Dynamic coverage planning</li>
                    <li><strong>Human-in-the-Loop</strong>: Review, edit quantity/supplier, or approve</li>
                    <li><strong>PO Export</strong>: Download CSV/Excel ready for MARG ERP import</li>
                    <li><strong>Promo Separation</strong>: Isolated non-commercial promotional materials</li>
                </ul>
            </div>
        </div>
        """, unsafe_allow_html=True)
        if st.button("🚀 Launch Procurement Application", key="launch_procurement_btn", type="primary", use_container_width=True):
            st.session_state["current_page"] = "procurement"
            st.rerun()

    with col_card2:
        st.markdown("""
        <div style="background-color: #F8FAFC; border: 2px solid #E2E8F0; border-radius: 14px; padding: 24px; min-height: 340px; display: flex; flex-direction: column; justify-content: space-between;">
            <div>
                <div style="font-size: 2.2rem; margin-bottom: 8px;">📈</div>
                <h3 style="color: #0F172A; margin: 0 0 8px 0; font-size: 1.35rem; font-weight: 700;">Sales Intelligence & Reorders</h3>
                <p style="color: #475569; font-size: 0.95rem; line-height: 1.5; margin-bottom: 16px;">
                    Actionable customer recommendations and financial outstanding intelligence.
                </p>
                <ul style="color: #64748B; font-size: 0.88rem; padding-left: 20px; line-height: 1.6; margin-bottom: 20px;">
                    <li><strong>Customer Financial Summary</strong>: Sales (30d/90d), current dues, oldest invoice</li>
                    <li><strong>⚠️ Near-Expiry Opportunities</strong>: Match warehouse expiry stock with customer purchase history</li>
                    <li><strong>Reorder Forecasting</strong>: Tell sales reps exactly what to ask the customer today</li>
                    <li><strong>Bill-wise Ageing</strong>: Current, 30d, 60d, 90d+ overdue invoice breakdown</li>
                    <li><strong>MARG Ingestion</strong>: Direct Excel import for Sales Register & Master</li>
                </ul>
            </div>
        </div>
        """, unsafe_allow_html=True)
        if st.button("📊 Launch Sales Intelligence", key="launch_sales_btn", type="primary", use_container_width=True):
            st.session_state["current_page"] = "sales"
            st.rerun()

    st.markdown("---")

    # Bottom helper cards
    col_h1, col_h2, col_h3 = st.columns(3)
    with col_h1:
        st.markdown("""
        **🔒 Local-First Privacy**  
        All database records & calculations run 100% locally on your machine via SQLite with WAL mode.
        """)
    with col_h2:
        st.markdown("""
        **🛡️ MARG ERP Compatibility**  
        Intelligent fuzzy column detection tolerates varying Excel formats, sheet layouts, and extra rows.
        """)
    with col_h3:
        st.markdown("""
        **⚡ Real-Time Recalculation**  
        Upload stock and sales exports anytime to update reorder points, dues, and expiry risk in seconds.
        """)

    st.markdown("---")
    st.markdown("### 🗑️ Database Management")
    st.caption("Clean-slate mode: Wipe all operational records from the database at any time before starting a new run or importing fresh data.")
    col_p1, col_p2 = st.columns([2, 1])
    with col_p1:
        st.markdown("""
        Clicking the button will **permanently wipe all operational data** from the database:
        - Catalog Products & Packaging Masters
        - Inventory Batches, Stock-On-Hand & Pipeline On-Order
        - Historical Sales Transactions & Demand Velocity
        - Supplier & Manufacturer Masters
        - Procurement Proposals, Approvals & Runs
        - Customer Ledgers & Outstanding Dues
        """)
    with col_p2:
        if st.button(
            "🗑️ Purge & Wipe Entire Database Now",
            key="btn_landing_purge_db",
            type="secondary",
            use_container_width=True,
            help="Wipes all operational tables (Products, Inventory, Sales, Suppliers, Proposals, Customers) to start 100% clean."
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
                    st.success("✅ Database purged completely! All operational tables cleared.")
                    st.rerun()
                else:
                    st.error(f"Error resetting database: {p_res.text}")
            except Exception as e:
                st.error(f"Reset error: {e}")

