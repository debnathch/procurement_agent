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
            c_res = requests.get(f"{BACKEND_URL}/sales/customers?limit=10000", timeout=15)
            if c_res.status_code == 200:
                customers = c_res.json()
        except Exception as e:
            st.error(f"Error loading customers from backend: {e}")

    # Section 1: Upload MARG Sales Excel & Demo Data
    expand_upload = st.session_state.get("expand_sales_upload", len(customers) == 0)
    with st.expander("📥 MARG Sales, Customer & Receivables Data Import Center", expanded=expand_upload):
        col_m_card, col_p_card = st.columns(2)

        # ----------------- UPLOADER 1: MASTER CUSTOMER LEDGER -----------------
        with col_m_card:
            st.markdown("""
            <div style="background-color: #F8FAFC; border: 1px solid #CBD5E1; border-radius: 8px; padding: 12px; margin-bottom: 8px;">
                <div style="font-size: 1.05rem; font-weight: 700; color: #1E293B;">📁 1. Master Customer Ledger</div>
                <div style="color: #475569; font-size: 0.82rem; margin-top: 3px;">
                    Extracts <b>Customer Name</b> (<i>Ledger</i>), <b>Category</b> (<i>Group</i>), <b>Customer Dues</b> (<i>Debit</i>), and <b>Company Liability</b> (<i>Credit</i>).
                </div>
            </div>
            """, unsafe_allow_html=True)

            master_file = st.file_uploader(
                "Upload Master Customer Ledger (.xlsx, .xls, .csv)",
                type=["xlsx", "xls", "csv"],
                key="sales_uploader_master_file",
                help="Upload MARG Customer Master list containing Ledger, Group, Debit, Credit columns."
            )

            col_m_btn1, col_m_btn2 = st.columns([1.3, 1])
            with col_m_btn1:
                do_upload_master = st.button(
                    "🚀 Ingest Customer Master",
                    type="primary",
                    key="btn_ingest_master",
                    use_container_width=True
                )
            with col_m_btn2:
                do_purge = st.button(
                    "🗑️ Purge Customer DB",
                    type="secondary",
                    key="btn_purge_sales",
                    use_container_width=True,
                    help="Permanently delete all customer records, dues, and transaction history."
                )

            if do_purge:
                with st.spinner("Purging customer database..."):
                    try:
                        res = requests.post(f"{BACKEND_URL}/sales/purge-customers", timeout=30)
                        if res.status_code == 200:
                            data = res.json()
                            purged = data.get("purged", {})
                            c_del = purged.get("deleted_customers", 0)
                            r_del = purged.get("deleted_receivables", 0)
                            s_del = purged.get("deleted_sales", 0)
                            st.session_state["sales_upload_banner"] = (
                                f"🗑️ Customer database purged! Removed {c_del} customer(s), "
                                f"{r_del} receivable record(s), and {s_del} sales row(s)."
                            )
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

            if do_upload_master:
                if not master_file:
                    st.warning("⚠️ Please select a Master Customer Ledger file above before clicking 'Ingest Customer Master'.")
                else:
                    with st.spinner(f"Ingesting Customer Master '{master_file.name}' into database..."):
                        try:
                            files = {"file": (master_file.name, master_file.getvalue())}
                            params = {"clear_existing": False}
                            res = requests.post(f"{BACKEND_URL}/sales/upload-marg", files=files, params=params, timeout=120)
                            if res.status_code == 200:
                                data = res.json()
                                stats = data.get("stats", {})
                                c_count = stats.get('customers_upserted', 0)
                                r_count = stats.get('receivables_upserted', 0)
                                if c_count == 0 and r_count == 0:
                                    st.session_state["sales_upload_error"] = (
                                        f"⚠️ File '{master_file.name}' was uploaded, but no customer records or dues columns were recognized. "
                                        f"Please ensure columns like 'Ledger' (Customer Name), 'Group', 'Debit', 'Credit' are present."
                                    )
                                else:
                                    st.session_state["sales_upload_banner"] = (
                                        f"✅ Successfully ingested Master Customer Ledger '{master_file.name}'! "
                                        f"Customers: {c_count}, Dues & Liabilities Recorded: {r_count}"
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

        # ----------------- UPLOADER 2: PRODUCT-WISE CUSTOMER LEDGER -----------------
        with col_p_card:
            st.markdown("""
            <div style="background-color: #F8FAFC; border: 1px solid #CBD5E1; border-radius: 8px; padding: 12px; margin-bottom: 8px;">
                <div style="font-size: 1.05rem; font-weight: 700; color: #1E293B;">📦 2. Product-Wise Customer Ledger</div>
                <div style="color: #475569; font-size: 0.82rem; margin-top: 3px;">
                    Extracts <b>Date, Party Name, Invoice No, Item/Particulars, Qty, Rate, and Amount</b>. Activates order history, near-expiry matching, and reorder intelligence.
                </div>
            </div>
            """, unsafe_allow_html=True)

            product_file = st.file_uploader(
                "Upload Product-Wise Sales Ledger (.xlsx, .xls, .csv)",
                type=["xlsx", "xls", "csv"],
                key="sales_uploader_prod_file",
                help="Upload MARG Customer Product Sales Ledger containing Date, Party Name, Particulars/Item, Qty, Rate, Amount."
            )

            do_upload_product = st.button(
                "🚀 Ingest Product-Wise Sales Ledger",
                type="primary",
                key="btn_ingest_product_ledger",
                use_container_width=True
            )

            if do_upload_product:
                if not product_file:
                    st.warning("⚠️ Please select a Product-Wise Sales Ledger file above before clicking 'Ingest Product-Wise Sales Ledger'.")
                else:
                    with st.spinner(f"Ingesting Product-Wise Ledger '{product_file.name}' into database..."):
                        try:
                            files = {"file": (product_file.name, product_file.getvalue())}
                            params = {"clear_existing": False}
                            res = requests.post(f"{BACKEND_URL}/sales/upload-marg", files=files, params=params, timeout=120)
                            if res.status_code == 200:
                                data = res.json()
                                stats = data.get("stats", {})
                                s_count = stats.get('sales_inserted', 0)
                                m_count = stats.get('matched_customers', 0)
                                c_count = stats.get('customers_upserted', 0)
                                if s_count == 0:
                                    st.session_state["sales_upload_error"] = (
                                        f"⚠️ File '{product_file.name}' was uploaded, but no product sales transactions were recognized. "
                                        f"Please ensure columns like Date, Party Name, Particulars / Item Description, Qty, Rate, or Amount are present."
                                    )
                                else:
                                    st.session_state["sales_upload_banner"] = (
                                        f"✅ Successfully ingested Product-Wise Customer Ledger '{product_file.name}'! "
                                        f"Sales Transactions: {s_count}, Customers Linked: {m_count or c_count}. "
                                        f"Order history, near-expiry matching, and recommendations are now activated!"
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

        # ----------------- DEMO DATA ROW -----------------
        st.markdown("---")
        col_demo1, col_demo2 = st.columns([3, 1])
        with col_demo1:
            st.caption("🧪 **Quick Demo Evaluation**: Need ready-made test data? Load realistic sample customers, category groups, dues (Debit), company liabilities (Credit), and historical product sales with near-expiry matches.")
        with col_demo2:
            if st.button("⚡ Load Demo Data", key="btn_seed_demo_sales", use_container_width=True):
                with st.spinner("Loading demo customers & sales..."):
                    try:
                        res = requests.post(f"{BACKEND_URL}/sales/seed-demo", timeout=15)
                        if res.status_code == 200:
                            st.session_state["sales_upload_banner"] = "✅ Demo customers, dues, and sales transactions loaded successfully!"
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
    group_counts = {}
    for c in customers:
        grp = str(c.get('group_name') or 'General').strip()
        group_counts[grp] = group_counts.get(grp, 0) + 1
        if grp and grp not in seen_groups:
            all_groups.append(grp)
            seen_groups.add(grp)
    all_groups.sort()

    # Format group options with live counts
    group_display_map = {
        "All": f"All Categories / Groups ({len(customers)})"
    }
    for g in all_groups:
        group_display_map[g] = f"{g} ({group_counts.get(g, 0)})"

    raw_group_choices = ["All"] + all_groups

    col_cat, col_cust = st.columns([1, 2])
    with col_cat:
        selected_group = st.selectbox(
            "1️⃣ Category / Group (from Excel 'Group'):",
            options=raw_group_choices,
            format_func=lambda x: group_display_map.get(x, x),
            key="sales_selected_group_dropdown"
        )

    # Filter customers by selected group
    if selected_group != "All":
        filtered_customers = [c for c in customers if str(c.get('group_name') or 'General').strip() == selected_group]
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
        dues = c.get('current_dues', 0.0)
        cr = c.get('company_payable', c.get('credit_amount', 0.0))
        tot_s = c.get('total_sales', 0.0)

        # Customer Dues is populated from Debit, Liable to Pay from Credit
        label = f"{name} [{grp}] | Dues (Debit): ₹{dues:,.2f} | Liable to Pay (Credit): ₹{cr:,.2f}"
        if tot_s > 0:
            label += f" | Sales: ₹{tot_s:,.2f}"
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
    current_dues = summary.get('current_dues', summary.get('current_outstanding', 0.0))
    company_payable = summary.get('company_payable', summary.get('credit_amount', 0.0))
    net_position = summary.get('net_receivable', current_dues - company_payable)
    oldest_days = summary.get('oldest_due_days', 0)
    oldest_inv = summary.get('oldest_due_invoice', 'None')

    tot_sales = summary.get('total_sales', 0.0)
    sales_30d = summary.get('sales_30d', 0.0)
    sales_90d = summary.get('sales_90d', 0.0)
    total_orders = summary.get('total_orders', 0)
    last_order_dt = summary.get('last_order_date', 'N/A')
    days_since_order = summary.get('days_since_last_order', 'N/A')
    avg_order_val = summary.get('average_order_value', 0.0)

    st.markdown("#### 💰 Financial Ledger & Dues Position")
    col_bal1, col_bal2, col_bal3, col_bal4 = st.columns(4)
    with col_bal1:
        st.metric(
            "Customer Dues (Debit)",
            f"₹{current_dues:,.2f}",
            help="Outstanding dues customer owes to the company (imported from Debit column)"
        )
    with col_bal2:
        st.metric(
            "Company Liable to Pay (Credit)",
            f"₹{company_payable:,.2f}",
            help="Credit balance / amount company is liable to pay or adjust (imported from Credit column)"
        )
    with col_bal3:
        if net_position >= 0:
            st.metric(
                "Net Position (Receivable)",
                f"₹{net_position:,.2f}",
                delta=f"+₹{net_position:,.2f} Net Due" if net_position > 0 else "Balanced (₹0.00)",
                delta_color="normal",
                help="Net customer dues = Debit Dues − Credit Liability"
            )
        else:
            st.metric(
                "Net Position (Payable)",
                f"₹{abs(net_position):,.2f}",
                delta=f"-₹{abs(net_position):,.2f} Company Liable",
                delta_color="inverse",
                help="Net company liability = Credit Liability − Debit Dues"
            )
    with col_bal4:
        due_str = f"{oldest_days} Days" if oldest_days > 0 else "0 Days"
        st.metric("Oldest Due Age", due_str, help=f"Oldest Due Voucher/Invoice: {oldest_inv}")

    st.markdown("#### 📊 Sales Activity & Turnover *(Populated via Product-Wise Customer Ledger)*")
    col_s1, col_s2, col_s3, col_s4 = st.columns(4)
    with col_s1:
        st.metric("Total Historical Sales", f"₹{tot_sales:,.2f}", help="Total billed sales turnover from product-wise customer ledger")
    with col_s2:
        st.metric("Sales (Last 30 Days)", f"₹{sales_30d:,.2f}")
    with col_s3:
        st.metric("Sales (Last 90 Days)", f"₹{sales_90d:,.2f}")
    with col_s4:
        st.metric("Total Orders / Invoices", total_orders, help=f"Last Order Date: {last_order_dt} ({days_since_order} days ago)")

    if total_orders == 0 and tot_sales == 0.0:
        st.info("ℹ️ **Product-Wise Customer Ledger Pending**: Sales figures, order volume, and transaction history will be populated when this customer's product-wise sales ledger (Uploader 2 above) is uploaded.")

    st.markdown("---")

    # Section 7: Detail Deep-Dive Tabs
    tab_expiry, tab_reorders, tab_history, tab_ageing = st.tabs([
        "⚠️ Near-Expiry Opportunities",
        "🎯 Reorder Suggestions",
        "📜 Purchase History",
        "💳 Customer Dues & Liabilities"
    ])

    # TAB 1: Near-Expiry Opportunities
    with tab_expiry:
        st.subheader("⚠️ Near-Expiry Opportunities")
        st.markdown("""
        Products this customer **historically purchases** that currently have warehouse batches expiring within **90 days**.
        Push suitable stock with complete confidence and clear rationales.
        """)

        near_opps = recommendations.get('near_expiry_opportunities', [])
        if total_orders == 0:
            st.info("ℹ️ **Product-Wise Customer Ledger Pending**: Near-expiry product opportunities match against products this customer historically purchases. Upload product-wise customer sales ledger (Uploader 2 above) to activate.")
        elif not near_opps:
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
        if total_orders == 0:
            st.info("ℹ️ **Product-Wise Customer Ledger Pending**: Reorder frequency, order patterns, and suggested replenishment quantities are calculated from product-wise sales transactions (Uploader 2 above).")
        elif not reorders:
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
        st.subheader("📜 Customer Purchase History & Transacted Products")
        st.markdown(
            "Complete product portfolio and sales transactions transacted by this customer, "
            "extracted from the **Product-Wise Customer Ledger**."
        )

        transacted_products = summary.get('transacted_products', [])
        recent_orders = summary.get('recent_orders', [])

        # Fallback aggregation in case frontend received raw recent_orders without transacted_products
        if not transacted_products and recent_orders:
            p_map = {}
            for r in recent_orders:
                p_code = r.get('product_code', 'UNKNOWN')
                p_name = r.get('product_name', p_code)
                qty = float(r.get('qty', 0.0) or 0.0)
                amt = float(r.get('amount', 0.0) or 0.0)
                rate = float(r.get('rate', 0.0) or 0.0)
                dt = r.get('date', 'N/A')
                if p_code not in p_map:
                    p_map[p_code] = {
                        'product_code': p_code,
                        'product_name': p_name,
                        'company': 'General',
                        'category': 'Tablets',
                        'total_qty': 0.0,
                        'total_amount': 0.0,
                        'order_count': 0,
                        'rates': [],
                        'last_purchase_date': dt,
                    }
                entry = p_map[p_code]
                entry['total_qty'] += qty
                entry['total_amount'] += amt
                entry['order_count'] += 1
                if rate > 0:
                    entry['rates'].append(rate)
                if dt != 'N/A':
                    entry['last_purchase_date'] = dt

            for p_code, p_info in p_map.items():
                rates = p_info['rates']
                avg_r = round(sum(rates) / len(rates), 2) if rates else (
                    round(p_info['total_amount'] / p_info['total_qty'], 2) if p_info['total_qty'] > 0 else 0.0
                )
                transacted_products.append({
                    'product_code': p_info['product_code'],
                    'product_name': p_info['product_name'],
                    'company': p_info['company'],
                    'category': p_info['category'],
                    'total_qty': round(p_info['total_qty'], 2),
                    'total_amount': round(p_info['total_amount'], 2),
                    'avg_rate': avg_r,
                    'order_count': p_info['order_count'],
                    'last_purchase_date': p_info['last_purchase_date'],
                })
            transacted_products.sort(key=lambda x: x['total_amount'], reverse=True)

        if not transacted_products and not recent_orders:
            st.info(
                "ℹ️ **No Product-Wise Customer Ledger Uploaded Yet**: "
                "The product list transacted by this customer, quantities, rates, and historical sales transactions "
                "will appear here once the **Product-Wise Customer Ledger** (.xlsx/.xls) is ingested using Uploader 2 above."
            )
        else:
            col_hp1, col_hp2, col_hp3, col_hp4 = st.columns(4)
            with col_hp1:
                st.metric("Unique Products Transacted", len(transacted_products))
            with col_hp2:
                tot_units = sum(p.get('total_qty', 0.0) for p in transacted_products)
                st.metric("Total Units Transacted", f"{tot_units:,.1f}")
            with col_hp3:
                tot_val = sum(p.get('total_amount', 0.0) for p in transacted_products)
                st.metric("Total Transacted Value", f"₹{tot_val:,.2f}")
            with col_hp4:
                st.metric("Total Invoices / Orders", total_orders)

            st.markdown("---")

            sub_tab_products, sub_tab_txns = st.tabs([
                f"📦 Transacted Products List ({len(transacted_products)} Products)",
                f"🧾 Detailed Invoices & Transactions ({len(recent_orders)} Records)"
            ])

            with sub_tab_products:
                st.markdown(
                    "**Customer Product Portfolio**: All products transacted by this customer from the **Product-Wise Customer Ledger**."
                )
                search_query = st.text_input(
                    "🔍 Filter Transacted Products:",
                    placeholder="Search by product name or item code...",
                    key=f"search_prods_{selected_code}"
                )

                filtered_prods = transacted_products
                if search_query and search_query.strip():
                    sq = search_query.strip().lower()
                    filtered_prods = [
                        p for p in transacted_products
                        if sq in str(p.get('product_name', '')).lower() or sq in str(p.get('product_code', '')).lower()
                    ]

                if not filtered_prods:
                    st.warning(f"No transacted products found matching '{search_query}'.")
                else:
                    df_prods = pd.DataFrame(filtered_prods)
                    cols_to_use = [
                        c for c in ['product_name', 'product_code', 'company', 'total_qty', 'avg_rate', 'total_amount', 'order_count', 'last_purchase_date']
                        if c in df_prods.columns
                    ]
                    df_display_prods = df_prods[cols_to_use].copy()
                    df_display_prods.rename(columns={
                        'product_name': 'Product Name',
                        'product_code': 'Item Code',
                        'company': 'Company / Brand',
                        'total_qty': 'Total Qty Transacted',
                        'avg_rate': 'Avg Rate (₹)',
                        'total_amount': 'Total Value (₹)',
                        'order_count': 'Order Frequency',
                        'last_purchase_date': 'Last Purchased',
                    }, inplace=True)

                    st.dataframe(df_display_prods, use_container_width=True, hide_index=True)

                    csv_prods = df_display_prods.to_csv(index=False).encode('utf-8')
                    st.download_button(
                        label=f"📥 Download Transacted Products CSV ({cust_name})",
                        data=csv_prods,
                        file_name=f"{selected_code}_transacted_products.csv",
                        mime="text/csv",
                        key=f"btn_dl_prods_{selected_code}"
                    )

            with sub_tab_txns:
                st.markdown("**Detailed Sales Transaction Log**: Line-by-line sales orders and invoice entries.")
                if not recent_orders:
                    st.info("No detailed transaction rows available.")
                else:
                    df_orders = pd.DataFrame(recent_orders)
                    rename_cols = {
                        'invoice_no': 'Invoice / Voucher No',
                        'date': 'Invoice Date',
                        'product_name': 'Product Name',
                        'product_code': 'Item Code',
                        'batch_no': 'Batch',
                        'qty': 'Quantity Sold',
                        'rate': 'Rate (₹)',
                        'amount': 'Net Amount (₹)',
                    }
                    avail_cols = [c for c in ['invoice_no', 'date', 'product_name', 'product_code', 'batch_no', 'qty', 'rate', 'amount'] if c in df_orders.columns]
                    df_display_orders = df_orders[avail_cols].copy()
                    df_display_orders.rename(columns=rename_cols, inplace=True)
                    st.dataframe(df_display_orders, use_container_width=True, hide_index=True)

                    csv_txns = df_display_orders.to_csv(index=False).encode('utf-8')
                    st.download_button(
                        label=f"📥 Download Invoices & Transactions CSV ({cust_name})",
                        data=csv_txns,
                        file_name=f"{selected_code}_sales_transactions.csv",
                        mime="text/csv",
                        key=f"btn_dl_txns_{selected_code}"
                    )

    # TAB 4: Customer Dues & Liabilities Ageing
    with tab_ageing:
        st.subheader("💳 Customer Dues (Debit) & Company Liability (Credit) Breakdown")
        st.markdown("""
        Ageing breakdown of customer dues (**Debit**) and credit balances the company is liable to pay (**Credit**).
        """)
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
            st.success("🎉 No customer dues or liability records found for this customer!")
        else:
            df_invs = pd.DataFrame(pending_invs)
            rename_map = {
                'invoice_no': 'Voucher / Bill No',
                'invoice_date': 'Bill Date',
                'due_date': 'Due Date',
                'outstanding_amount': 'Customer Dues / Debit (₹)',
                'company_payable': 'Company Liable to Pay / Credit (₹)',
                'days_due': 'Days Overdue',
                'ageing_bucket': 'Ageing Bracket',
            }
            display_cols = [c for c in ['invoice_no', 'invoice_date', 'due_date', 'outstanding_amount', 'company_payable', 'days_due', 'ageing_bucket'] if c in df_invs.columns]
            df_display = df_invs[display_cols].copy()
            df_display.rename(columns=rename_map, inplace=True)
            st.dataframe(df_display, use_container_width=True, hide_index=True)
