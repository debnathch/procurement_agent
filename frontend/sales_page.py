"""
Sales Intelligence & Customer Recommendations UI Module.

Features:
- MARG Excel Upload (Sales Register, Customer Master, Bill-wise Outstanding)
- Searchable Customer dropdown from MARG
- Customer Financial Summary (Total Historical Sales, Current Outstanding/Dues, 30d/90d sales, Oldest Due)
- Near-Expiry Opportunities matching customer's historical purchases
- Reorder Candidates & Purchase Frequency
- Bill-wise Outstanding Ageing Analysis
"""
import requests
import streamlit as st
import pandas as pd


def render_sales_page(BACKEND_URL: str, is_healthy: bool):
    # Top Action Bar
    col_nav1, col_nav2 = st.columns([1, 5])
    with col_nav1:
        if st.button("🏠 Back to Dashboard", key="btn_sales_back_top", use_container_width=True):
            st.session_state["current_page"] = "landing"
            st.rerun()

    with col_nav2:
        st.markdown('<div class="main-header">📈 Pharma Sales Intelligence & Customer Recommendations</div>', unsafe_allow_html=True)
        st.markdown('<div class="sub-header">Customer buying behavior, financial dues, near-expiry opportunities, and reorder intelligence.</div>', unsafe_allow_html=True)

    st.markdown("---")

    # Session Banners
    if "sales_upload_banner" in st.session_state and st.session_state["sales_upload_banner"]:
        st.success(st.session_state.pop("sales_upload_banner"))
    if "sales_upload_error" in st.session_state and st.session_state["sales_upload_error"]:
        st.error(st.session_state.pop("sales_upload_error"))

    # Fetch Customer List first to know if we need the import center expanded
    customers = []
    if is_healthy:
        try:
            c_res = requests.get(f"{BACKEND_URL}/sales/customers", timeout=10)
            if c_res.status_code == 200:
                customers = c_res.json()
        except Exception as e:
            st.error(f"Error loading customers from backend: {e}")

    # Section 1: Upload MARG Sales Excel & Demo Data
    expand_upload = st.session_state.get("expand_sales_upload", len(customers) == 0)
    with st.expander("📥 MARG Sales, Customer & Receivables Data Import Center", expanded=expand_upload):
        col_up1, col_up2 = st.columns([2, 1])
        with col_up1:
            sales_file = st.file_uploader(
                "Select MARG Sales Register, Customer Master, or Bill-wise Outstanding (.xlsx, .xls, .csv)",
                type=["xlsx", "xls", "csv"],
                key="sales_uploader_file"
            )
            col_f1, col_f2 = st.columns([1, 1])
            with col_f1:
                do_upload = st.button("🚀 Ingest MARG Data", type="primary", key="btn_ingest_sales", use_container_width=True)
            with col_f2:
                do_purge = st.button("🗑️ Purge Customer Database", type="secondary", key="btn_purge_sales", use_container_width=True, help="Permanently delete all customer records and outstanding dues.")

            if do_purge:
                with st.spinner("Purging customer database..."):
                    try:
                        res = requests.post(f"{BACKEND_URL}/sales/purge-customers", timeout=30)
                        if res.status_code == 200:
                            data = res.json()
                            purged = data.get("purged", {})
                            c_del = purged.get("deleted_customers", 0)
                            r_del = purged.get("deleted_receivables", 0)
                            st.session_state["sales_upload_banner"] = f"🗑️ Customer database purged! Removed {c_del} customer(s) and {r_del} receivable record(s)."
                            st.session_state["expand_sales_upload"] = True
                            st.rerun()
                        else:
                            st.session_state["sales_upload_error"] = f"Purge failed ({res.status_code}): {res.text}"
                            st.session_state["expand_sales_upload"] = True
                            st.rerun()
                    except Exception as e:
                        st.session_state["sales_upload_error"] = f"Error communicating with backend: {e}"
                        st.session_state["expand_sales_upload"] = True
                        st.rerun()

            if do_upload:
                if not sales_file:
                    st.warning("⚠️ Please select an Excel file (.xlsx, .xls, .csv) above before clicking 'Ingest MARG Data'.")
                else:
                    with st.spinner(f"Ingesting '{sales_file.name}' into database..."):
                        try:
                            files = {"file": (sales_file.name, sales_file.getvalue())}
                            params = {"clear_existing": False}
                            res = requests.post(f"{BACKEND_URL}/sales/upload-marg", files=files, params=params, timeout=120)
                            if res.status_code == 200:
                                data = res.json()
                                stats = data.get("stats", {})
                                c_count = stats.get('customers_upserted', 0)
                                s_count = stats.get('sales_inserted', 0)
                                r_count = stats.get('receivables_upserted', 0)
                                if c_count == 0 and s_count == 0 and r_count == 0:
                                    st.session_state["sales_upload_error"] = (
                                        f"⚠️ File '{sales_file.name}' was uploaded, but no customer records or sales/dues columns could be recognized. "
                                        f"Please ensure columns like Party Name, Total Sales, or Dues are present."
                                    )
                                else:
                                    st.session_state["sales_upload_banner"] = (
                                        f"✅ Successfully ingested '{sales_file.name}'! "
                                        f"Customers: {c_count}, Sales Lines: {s_count}, Receivables: {r_count}"
                                    )
                                st.session_state["expand_sales_upload"] = False
                                st.rerun()
                            else:
                                st.session_state["sales_upload_error"] = f"Upload failed ({res.status_code}): {res.text}"
                                st.session_state["expand_sales_upload"] = True
                                st.rerun()
                        except Exception as e:
                            st.session_state["sales_upload_error"] = f"Error communicating with backend: {e}"
                            st.session_state["expand_sales_upload"] = True
                            st.rerun()

        with col_up2:
            st.markdown("#### 🧪 Quick Demo Data")
            st.caption("Instantly load realistic sample MARG customers, receivables, and sales history for testing.")
            if st.button("Load Demo Customers & Dues", key="btn_seed_demo_sales", use_container_width=True):
                with st.spinner("Loading demo customers..."):
                    try:
                        res = requests.post(f"{BACKEND_URL}/sales/seed-demo", timeout=15)
                        if res.status_code == 200:
                            st.session_state["sales_upload_banner"] = "✅ Demo customers, receivables, and sales history loaded successfully!"
                            st.session_state["expand_sales_upload"] = False
                            st.rerun()
                        else:
                            st.session_state["sales_upload_error"] = f"Failed to load demo data: {res.text}"
                            st.rerun()
                    except Exception as e:
                        st.session_state["sales_upload_error"] = f"Error communicating with backend: {e}"
                        st.rerun()

    # Section 2: Customer Selection
    if not customers:
        st.info("ℹ️ No customer records found. Please expand the upload section above to upload a MARG Sales/Customer file or click 'Load Demo Customers & Dues'.")
        return

    # Section 3: Cascading Category / Group and Customer Selector Dropdowns
    st.markdown("### 👤 Select Customer by Category / Group")

    # Extract distinct groups from customer list (populated from Excel 'Group' column)
    all_groups = []
    seen_groups = set()
    for c in customers:
        grp = c.get('group_name')
        if grp and str(grp).strip() and str(grp).strip() not in seen_groups:
            all_groups.append(str(grp).strip())
            seen_groups.add(str(grp).strip())
    all_groups.sort()

    col_cat, col_cust = st.columns([1, 2])
    with col_cat:
        group_options = ["All Categories / Groups"] + all_groups
        selected_group = st.selectbox(
            "1️⃣ Category / Group (from Excel 'Group'):",
            options=group_options,
            key="sales_selected_group_dropdown"
        )

    # Filter customers by selected group
    if selected_group != "All Categories / Groups":
        filtered_customers = [c for c in customers if c.get('group_name') == selected_group]
    else:
        filtered_customers = customers

    if not filtered_customers:
        st.warning(f"No customers found in category '{selected_group}'.")
        return

    cust_options = []
    cust_code_map = {}
    for c in filtered_customers:
        code = c['customer_code']
        name = c['customer_name']
        dist = c.get('district', 'General')
        grp = c.get('group_name') or 'General'
        tot_s = c.get('total_sales', 0.0)
        dues = c.get('current_dues', 0.0)
        label = f"{name} ({code}) — [{grp} | {dist}] | Sales: ₹{tot_s:,.0f} | Dues: ₹{dues:,.0f}"
        cust_options.append(label)
        cust_code_map[label] = code

    with col_cust:
        selected_label = st.selectbox(
            f"2️⃣ Customer ({len(filtered_customers)} in this category):",
            options=cust_options,
            key="sales_selected_customer_label"
        )

    selected_code = cust_code_map.get(selected_label)
    if not selected_code:
        return

    # Section 4: Fetch Customer Summary
    summary = {}
    recommendations = {}
    try:
        s_res = requests.get(f"{BACKEND_URL}/sales/customers/{selected_code}/summary", timeout=10)
        if s_res.status_code == 200:
            summary = s_res.json()

        r_res = requests.get(f"{BACKEND_URL}/sales/customers/{selected_code}/recommendations", timeout=10)
        if r_res.status_code == 200:
            recommendations = r_res.json()
    except Exception as e:
        st.error(f"Error fetching customer details: {e}")
        return

    # Section 5: Customer Profile Card
    cust_name = summary.get('customer_name', selected_code)
    cust_group = summary.get('group_name', 'General')
    cust_district = summary.get('district', 'West Bengal')
    cust_salesperson = summary.get('salesperson', 'Sales Team')
    cust_credit_limit = summary.get('credit_limit', 0.0)

    st.markdown(f"""
    <div style="background-color: #F8FAFC; border: 1px solid #E2E8F0; border-radius: 10px; padding: 16px; margin-top: 12px; margin-bottom: 20px;">
        <div style="font-size: 1.4rem; font-weight: 700; color: #1E293B;">🏢 {cust_name}</div>
        <div style="color: #64748B; font-size: 0.95rem; margin-top: 4px;">
            <b>Customer Code:</b> <code>{selected_code}</code> &nbsp;|&nbsp;
            <b>Category / Group:</b> <span style="background-color: #E0E7FF; color: #4338CA; padding: 2px 8px; border-radius: 4px; font-weight: 600; font-size: 0.85rem;">{cust_group}</span> &nbsp;|&nbsp;
            <b>District:</b> {cust_district} &nbsp;|&nbsp;
            <b>Salesperson:</b> {cust_salesperson} &nbsp;|&nbsp;
            <b>Credit Limit:</b> ₹{cust_credit_limit:,.2f}
        </div>
    </div>
    """, unsafe_allow_html=True)

    # Section 6: Key Financial Summary Metrics
    tot_sales = summary.get('total_sales', 0.0)
    current_dues = summary.get('current_outstanding', 0.0)
    sales_30d = summary.get('sales_30d', 0.0)
    sales_90d = summary.get('sales_90d', 0.0)
    total_orders = summary.get('total_orders', 0)
    oldest_days = summary.get('oldest_due_days', 0)
    oldest_inv = summary.get('oldest_due_invoice', 'None')
    last_order_dt = summary.get('last_order_date', 'N/A')
    days_since_order = summary.get('days_since_last_order', 'N/A')
    avg_order_val = summary.get('average_order_value', 0.0)

    col_f1, col_f2, col_f3, col_f4 = st.columns(4)
    with col_f1:
        st.metric("Total Historical Sales", f"₹{tot_sales:,.2f}")
        st.metric("Sales (Last 30 Days)", f"₹{sales_30d:,.2f}")
    with col_f2:
        st.metric("Current Outstanding Dues", f"₹{current_dues:,.2f}")
        st.metric("Sales (Last 90 Days)", f"₹{sales_90d:,.2f}")
    with col_f3:
        due_str = f"{oldest_days} Days" if oldest_days > 0 else "0 Days"
        st.metric("Oldest Due Age", due_str, help=f"Oldest Invoice: {oldest_inv}")
        st.metric("Total Invoices / Orders", total_orders)
    with col_f4:
        st.metric("Last Order Date", str(last_order_dt), help=f"Days Since Last Order: {days_since_order}")
        st.metric("Average Order Value", f"₹{avg_order_val:,.2f}")

    st.markdown("---")

    # Section 7: Detail Deep-Dive Tabs
    tab_expiry, tab_reorders, tab_history, tab_ageing = st.tabs([
        "⚠️ Near-Expiry Opportunities",
        "🎯 Reorder Suggestions",
        "📜 Purchase History",
        "💳 Bill-Wise Outstanding Ageing"
    ])

    # TAB 1: Near-Expiry Opportunities
    with tab_expiry:
        st.subheader("⚠️ Near-Expiry Opportunities")
        st.markdown("""
        Products this customer **historically purchases** that currently have warehouse batches expiring within **90 days**.
        Push suitable stock with complete confidence and clear rationales.
        """)

        near_opps = recommendations.get('near_expiry_opportunities', [])
        if not near_opps:
            st.info("✅ No near-expiry products currently matching this customer's historical purchasing profile.")
        else:
            for opp in near_opps:
                is_crit = opp.get('is_critical', False)
                badge_style = "badge-fefo-danger" if is_crit else "badge-fefo-warn"
                crit_label = "🚨 CRITICAL EXPIRY (<= 30 DAYS)" if is_crit else "⚠️ NEAR-EXPIRY"

                with st.container():
                    st.markdown(f"""
                    <div style="border: 1px solid #CBD5E1; border-radius: 8px; padding: 14px; margin-bottom: 12px; background: white;">
                        <span class="{badge_style}">{crit_label}</span>
                        <div style="font-size: 1.15rem; font-weight: 700; color: #1E293B; margin-top: 4px;">
                            {opp['product_name']} <span style="font-size: 0.85rem; color: #64748B;">({opp['product_code']})</span>
                        </div>
                        <div style="color: #475569; font-size: 0.9rem; margin-top: 4px;">
                            <b>Company:</b> {opp['company']} &nbsp;|&nbsp;
                            <b>Batch:</b> <code>{opp['batch_no']}</code> &nbsp;|&nbsp;
                            <b>Expiry:</b> {opp['expiry_date']} (<b>{opp['days_remaining']} days remaining</b>)
                        </div>
                        <div style="color: #475569; font-size: 0.9rem; margin-top: 2px;">
                            <b>Available Stock:</b> {opp['current_stock']:g} units &nbsp;|&nbsp;
                            <b>Customer Typical Purchase:</b> {opp['customer_typical_qty']:g} units &nbsp;|&nbsp;
                            <b>Last Purchase:</b> {opp['last_purchase']}
                        </div>
                        <div style="background-color: #F1F5F9; border-left: 4px solid #3B82F6; padding: 8px 12px; margin-top: 8px; font-size: 0.9rem; color: #1E293B;">
                            <b>💡 Reason:</b> {opp['reason']}
                        </div>
                    </div>
                    """, unsafe_allow_html=True)

                    c_act1, c_act2, c_act3 = st.columns([1.5, 1.5, 3])
                    with c_act1:
                        if st.button(f"➕ Add {opp['suggested_qty']:g} to Order", key=f"btn_add_opp_{opp['product_code']}_{opp['batch_no']}"):
                            st.success(f"Added {opp['suggested_qty']:g} units of {opp['product_name']} to proposed order!")
                    with c_act2:
                        if st.button("💬 Ask Customer", key=f"btn_ask_opp_{opp['product_code']}_{opp['batch_no']}"):
                            st.info(f"Marked {opp['product_name']} for sales call inquiry.")
                    with c_act3:
                        if st.button("❌ Ignore", key=f"btn_ign_opp_{opp['product_code']}_{opp['batch_no']}"):
                            st.caption("Opportunity dismissed.")

    # TAB 2: Reorder Candidates
    with tab_reorders:
        st.subheader("🎯 Regular Reorder Candidates ('What to Ask Today')")
        reorders = recommendations.get('regular_reorders', [])
        if not reorders:
            st.info("No reorders currently due based on expected reorder cycles.")
        else:
            df_reorder = pd.DataFrame([
                {
                    'Product Name': r['product_name'],
                    'Product Code': r['product_code'],
                    'Company': r['company'],
                    'Order Count': r['order_count'],
                    'Typical Qty': r['typical_qty'],
                    'Suggested Qty': r['suggested_qty'],
                    'Last Purchase': r['last_purchase'],
                    'Reorder Interval': r['reorder_interval'],
                    'Available Stock': r['available_stock'],
                    'Priority': r['priority'],
                    'Reason': r['reason'],
                }
                for r in reorders
            ])
            st.dataframe(df_reorder, use_container_width=True, hide_index=True)

    # TAB 3: Purchase History
    with tab_history:
        st.subheader("📜 Historical Sales Transactions")
        recent_orders = summary.get('recent_orders', [])
        if not recent_orders:
            st.info("No recorded historical sales lines found for this customer.")
        else:
            df_orders = pd.DataFrame(recent_orders)
            df_orders.rename(columns={
                'invoice_no': 'Invoice No',
                'date': 'Invoice Date',
                'product_code': 'Item Code',
                'product_name': 'Product Name',
                'batch_no': 'Batch',
                'qty': 'Quantity Sold',
                'rate': 'Rate (₹)',
                'amount': 'Net Amount (₹)',
            }, inplace=True)
            st.dataframe(df_orders, use_container_width=True, hide_index=True)

    # TAB 4: Outstanding Ageing
    with tab_ageing:
        st.subheader("💳 Bill-Wise Outstanding Ageing Breakdown")
        ageing = summary.get('ageing', {})

        col_a1, col_a2, col_a3, col_a4, col_a5 = st.columns(5)
        with col_a1:
            st.metric("Current", f"₹{ageing.get('current', 0.0):,.2f}")
        with col_a2:
            st.metric("1–30 Days", f"₹{ageing.get('1_30', 0.0):,.2f}")
        with col_a3:
            st.metric("31–60 Days", f"₹{ageing.get('31_60', 0.0):,.2f}")
        with col_a4:
            st.metric("61–90 Days", f"₹{ageing.get('61_90', 0.0):,.2f}")
        with col_a5:
            st.metric("> 90 Days", f"₹{ageing.get('gt_90', 0.0):,.2f}")

        st.markdown("---")
        pending_invs = summary.get('pending_invoices', [])
        if not pending_invs:
            st.success("🎉 No outstanding dues or unpaid invoices pending for this customer!")
        else:
            df_invs = pd.DataFrame(pending_invs)
            df_invs.rename(columns={
                'invoice_no': 'Invoice Number',
                'invoice_date': 'Bill Date',
                'due_date': 'Due Date',
                'invoice_amount': 'Bill Amount (₹)',
                'outstanding_amount': 'Pending Dues (₹)',
                'days_due': 'Days Overdue',
                'ageing_bucket': 'Ageing Bracket',
            }, inplace=True)
            st.dataframe(df_invs, use_container_width=True, hide_index=True)
