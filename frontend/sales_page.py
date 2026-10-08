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

    # Section 2: Master Customer Category Filter & Sales Calculations
    if not customers:
        st.info("ℹ️ No customer records found. Please expand the upload section above to upload a MARG Sales/Customer file or click 'Load Demo Customers & Dues'.")
        return

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

    group_display_map = {
        "All": f"All Customer Categories ({len(customers)} Total Accounts)"
    }
    for g in all_groups:
        group_display_map[g] = f"{g} ({group_counts.get(g, 0)} Accounts)"

    raw_group_choices = ["All"] + all_groups

    st.markdown("""
    <div style="background-color: #F8FAFC; border-left: 5px solid #0284C7; border: 1px solid #CBD5E1; border-left-width: 5px; border-radius: 8px; padding: 12px 16px; margin-bottom: 12px;">
        <div style="font-size: 1.0rem; font-weight: 700; color: #0F172A;">
            🎯 Filter All Sales & Calculations by Customer Category
        </div>
        <div style="font-size: 0.83rem; color: #475569; margin-top: 2px;">
            Choose a customer category below. All sales KPIs, customer counts, outstanding dues, and revenue slab calculations will immediately recalculate for the selected category.
        </div>
    </div>
    """, unsafe_allow_html=True)

    col_cat_sel, col_cat_badge = st.columns([2, 3])
    with col_cat_sel:
        selected_group = st.selectbox(
            "🏷️ Customer Category / Group (from Excel 'Group'):",
            options=raw_group_choices,
            format_func=lambda x: group_display_map.get(x, x),
            key="sales_selected_group_dropdown"
        )

    # Filter customers by selected group
    if selected_group != "All":
        category_customers = [c for c in customers if str(c.get('group_name') or 'General').strip() == selected_group]
    else:
        category_customers = customers

    # Fetch or compute overview metrics dynamically for the selected category
    overview_data = None
    if is_healthy:
        try:
            params = {}
            if selected_group != "All":
                params["group"] = selected_group
            o_res = requests.get(f"{BACKEND_URL}/sales/overview-metrics", params=params, timeout=10)
            if o_res.status_code == 200:
                overview_data = o_res.json()
        except Exception:
            pass

    if not overview_data:
        tot_c = len(category_customers)
        tot_s = sum(c.get('total_sales', 0.0) for c in category_customers)
        tot_d = sum(c.get('current_dues', 0.0) for c in category_customers)
        tot_p = sum(c.get('company_payable', c.get('credit_amount', 0.0)) for c in category_customers)
        act_c = sum(1 for c in category_customers if c.get('total_sales', 0.0) > 0)

        specs = [
            ('0_to_2L', '0 to 2 Lakh', 0.0, 200000.0),
            ('2_to_5L', '>2 to 5 Lakh', 200000.0, 500000.0),
            ('5_to_8L', '>5 to 8 Lakh', 500000.0, 800000.0),
            ('8_to_12L', '>8 to 12 Lakh', 800000.0, 1200000.0),
            ('12_to_20L', '>12 to 20 Lakh', 1200000.0, 2000000.0),
            ('above_20L', '>20 Lakh', 2000000.0, float('inf')),
        ]
        bins_list = []
        for bid, blabel, bmin, bmax in specs:
            if bmax == float('inf'):
                in_b = [c for c in category_customers if c.get('total_sales', 0.0) > bmin]
            elif bmin == 0.0:
                in_b = [c for c in category_customers if 0.0 <= c.get('total_sales', 0.0) <= bmax]
            else:
                in_b = [c for c in category_customers if bmin < c.get('total_sales', 0.0) <= bmax]
            b_cnt = len(in_b)
            b_sales = sum(c.get('total_sales', 0.0) for c in in_b)
            bins_list.append({
                'bin_id': bid,
                'bin_label': blabel,
                'customer_count': b_cnt,
                'customer_percentage': round((b_cnt / tot_c * 100.0), 2) if tot_c > 0 else 0.0,
                'total_sales': round(b_sales, 2),
                'sales_percentage': round((b_sales / tot_s * 100.0), 2) if tot_s > 0 else 0.0,
                'customer_codes': [c['customer_code'] for c in in_b],
            })
        zero_sales_dues_custs = [c for c in category_customers if c.get('total_sales', 0.0) == 0.0 and c.get('current_dues', 0.0) > 0.0]
        overview_data = {
            'total_customers': tot_c,
            'active_customers': act_c,
            'inactive_customers': tot_c - act_c,
            'total_sales': round(tot_s, 2),
            'total_dues': round(tot_d, 2),
            'total_payable': round(tot_p, 2),
            'net_receivable': round(tot_d - tot_p, 2),
            'total_orders': sum(c.get('total_orders', 0) for c in category_customers),
            'zero_sales_dues_count': len(zero_sales_dues_custs),
            'zero_sales_dues_amount': round(sum(c.get('current_dues', 0.0) for c in zero_sales_dues_custs), 2),
            'zero_sales_dues_codes': [c['customer_code'] for c in zero_sales_dues_custs],
            'customer_sales_bins': bins_list,
        }

    # Top Executive KPI Cards (Filtered by Category)
    cat_title_suffix = f" — {selected_group}" if selected_group != "All" else " (All Categories)"
    st.markdown(f"### 📊 Sales Overview & Calculations{cat_title_suffix}")
    col_k1, col_k2, col_k3, col_k4 = st.columns(4)
    with col_k1:
        st.metric(
            "👥 Total Customers",
            f"{overview_data['total_customers']:,}",
            delta=f"{overview_data['active_customers']} Active Buyers",
            help=f"Total customer accounts in {selected_group} across MARG Master Ledger & Product Sales Ledger."
        )
    with col_k2:
        tot_sales_val = overview_data['total_sales']
        tot_sales_lakh = tot_sales_val / 100000.0
        st.metric(
            "💰 Total Sale",
            f"₹{tot_sales_val:,.2f}",
            delta=f"₹{tot_sales_lakh:.2f} Lakhs",
            help=f"Aggregate sales turnover for customer category '{selected_group}'."
        )
    with col_k3:
        tot_dues_val = overview_data['total_dues']
        tot_dues_lakh = tot_dues_val / 100000.0
        st.metric(
            "💳 Customer Dues (Debit)",
            f"₹{tot_dues_val:,.2f}",
            delta=f"₹{tot_dues_lakh:.2f} Lakhs",
            help=f"Total outstanding amount owed by customers in '{selected_group}' (Debit)."
        )
    with col_k4:
        tot_pay_val = overview_data['total_payable']
        tot_pay_lakh = tot_pay_val / 100000.0
        st.metric(
            "🏢 Company Payable (Credit)",
            f"₹{tot_pay_val:,.2f}",
            delta=f"₹{tot_pay_lakh:.2f} Lakhs",
            help=f"Total liability / credit amount company owes to customers in '{selected_group}' (Credit)."
        )

    # Customer Categorization Widget by Sales Slabs (Filtered by Category)
    st.markdown(f"""
    <div style="background-color: #F8FAFC; border: 1px solid #CBD5E1; border-radius: 10px; padding: 12px 16px; margin-top: 14px; margin-bottom: 12px;">
        <div style="display: flex; justify-content: space-between; align-items: center;">
            <div style="font-size: 1.05rem; font-weight: 700; color: #0F172A;">
                🏷️ Customer Category by Sales Slabs{cat_title_suffix}
            </div>
            <div style="font-size: 0.82rem; color: #475569; font-weight: 600;">
                Bins: 0–2 Lakh | >2–5 Lakh | >5–8 Lakh | >8–12 Lakh | >12–20 Lakh | >20 Lakh
            </div>
        </div>
        <div style="font-size: 0.83rem; color: #475569; margin-top: 3px;">
            Distribution of customers in <b>{selected_group}</b> segmented by historical purchases. Click any slab to filter customer selection.
        </div>
    </div>
    """, unsafe_allow_html=True)

    bins_list = overview_data.get('customer_sales_bins', [])
    col_b = st.columns(len(bins_list))
    color_accents = ["#0284C7", "#0D9488", "#16A34A", "#D97706", "#EA580C", "#7C3AED"]

    for idx, b_item in enumerate(bins_list):
        with col_b[idx]:
            b_cnt = b_item['customer_count']
            b_sales = b_item['total_sales']
            b_sales_l = b_sales / 100000.0
            b_cpct = b_item['customer_percentage']
            b_spct = b_item['sales_percentage']
            border_c = color_accents[idx % len(color_accents)]

            st.markdown(f"""
            <div style="background: white; border-top: 4px solid {border_c}; border: 1px solid #E2E8F0; border-top-color: {border_c}; border-radius: 8px; padding: 10px; box-shadow: 0 1px 3px rgba(0,0,0,0.06); text-align: center; min-height: 110px;">
                <div style="font-size: 0.82rem; font-weight: 700; color: #334155; margin-bottom: 2px;">{b_item['bin_label']}</div>
                <div style="font-size: 1.25rem; font-weight: 800; color: {border_c};">{b_cnt} <span style="font-size: 0.72rem; font-weight: 500; color: #64748B;">({b_cpct}%)</span></div>
                <div style="font-size: 0.85rem; font-weight: 700; color: #1E293B; margin-top: 4px;">₹{b_sales_l:,.2f} L</div>
                <div style="font-size: 0.70rem; color: #64748B;">{b_spct}% of sales</div>
            </div>
            """, unsafe_allow_html=True)

    # Interactive Slab Filter
    bin_options = ["All Slabs"] + [b['bin_label'] for b in bins_list]
    bin_label_to_codes = {b['bin_label']: set(b['customer_codes']) for b in bins_list}

    selected_slab = st.segmented_control(
        f"🎯 Filter Customers in {selected_group} by Sales Slab:",
        options=bin_options,
        default="All Slabs",
        key=f"sales_selected_slab_control_{selected_group}"
    )

    st.markdown("---")

    # SUNDRY DEBTORS Special Intelligence Placeholder & Filter (Enabled ONLY when Category is SUNDRY DEBTORS)
    is_sundry_debtors = ("SUNDRY DEBTORS" in selected_group.strip().upper())
    debtor_filter_criterion = "All"
    zero_sales_dues_list = []
    active_buyers_list = []

    if is_sundry_debtors:
        zero_sales_dues_list = [
            c for c in category_customers
            if c.get('total_sales', 0.0) == 0.0 and c.get('current_dues', 0.0) > 0.0
        ]
        active_buyers_list = [c for c in category_customers if c.get('total_sales', 0.0) > 0.0]
        z_count = len(zero_sales_dues_list)
        z_dues_tot = sum(c.get('current_dues', 0.0) for c in zero_sales_dues_list)
        z_dues_lakh = z_dues_tot / 100000.0
        avg_due = (z_dues_tot / z_count) if z_count > 0 else 0.0
        max_due = max([c.get('current_dues', 0.0) for c in zero_sales_dues_list], default=0.0)

        # Highlighted visual placeholder container for Sundry Debtors
        st.markdown(f"""
        <div style="background: linear-gradient(135deg, #FFF1F2 0%, #FFFBEB 100%); border: 1.5px solid #FCA5A5; border-radius: 10px; padding: 14px 18px; margin-top: 10px; margin-bottom: 14px; box-shadow: 0 1px 3px rgba(0,0,0,0.05);">
            <div style="display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 8px;">
                <div style="font-size: 1.05rem; font-weight: 700; color: #991B1B;">
                    🚨 SUNDRY DEBTORS Intelligence: Zero-Sales Parties with Positive Dues
                </div>
                <div style="display: flex; gap: 8px;">
                    <span style="font-size: 0.85rem; font-weight: 700; color: #991B1B; background: #FEE2E2; border: 1px solid #FCA5A5; padding: 3px 10px; border-radius: 6px;">
                        👥 {z_count} Debtors with ₹0 Sales
                    </span>
                    <span style="font-size: 0.85rem; font-weight: 700; color: #B45309; background: #FEF3C7; border: 1px solid #FCD34D; padding: 3px 10px; border-radius: 6px;">
                        💰 ₹{z_dues_lakh:.2f} Lakhs Dues Locked
                    </span>
                </div>
            </div>
            <div style="font-size: 0.83rem; color: #475569; margin-top: 4px;">
                Identified <b>{z_count} registered parties</b> in <b>SUNDRY DEBTORS</b> who have placed <b>no purchase orders (Sales = ₹0)</b> in the uploaded period, yet owe an aggregate <b>₹{z_dues_tot:,.2f}</b> in unrecovered debit dues. Use the filtering criteria below to isolate or view their complete roster.
            </div>
        </div>
        """, unsafe_allow_html=True)

        # Dedicated Filtering Criteria Option for SUNDRY DEBTORS
        debtor_filter_criterion = st.radio(
            "🎯 Filter SUNDRY DEBTORS Customer List By:",
            options=[
                f"All Sundry Debtors ({len(category_customers)} Accounts)",
                f"🚨 Zero Sales with Dues > ₹0 ONLY ({z_count} Accounts | ₹{z_dues_lakh:.2f}L Dues)",
                f"🛒 Active Buyers Only ({len(active_buyers_list)} Accounts with Sales > 0)"
            ],
            index=0,
            horizontal=True,
            key=f"debtor_filter_criterion_{selected_group}"
        )

        # Expandable Roster / Placeholder Table of parties with sales 0 and dues > 0
        with st.expander(
            f"📋 View Roster of Parties with Sales = 0 & Dues > 0 ({z_count} Accounts — ₹{z_dues_lakh:.2f} Lakhs Dues)",
            expanded=debtor_filter_criterion.startswith("🚨")
        ):
            col_rm1, col_rm2, col_rm3, col_rm4 = st.columns(4)
            with col_rm1:
                st.metric("Total Dormant Debtors", f"{z_count}")
            with col_rm2:
                st.metric("Total Dues Locked", f"₹{z_dues_tot:,.2f}", delta=f"₹{z_dues_lakh:.2f} Lakhs")
            with col_rm3:
                st.metric("Average Due per Party", f"₹{avg_due:,.2f}")
            with col_rm4:
                st.metric("Highest Single Due", f"₹{max_due:,.2f}")

            df_zero = pd.DataFrame([
                {
                    "Party Name": c['customer_name'],
                    "Customer Code": c['customer_code'],
                    "Outstanding Dues (Debit ₹)": c['current_dues'],
                    "Company Payable (Credit ₹)": c['company_payable'],
                    "Net Due (₹)": c['net_receivable'],
                    "Oldest Due (Days)": c.get('oldest_due_days', 0),
                    "Salesperson / Area": f"{c.get('salesperson', 'Unassigned')} ({c.get('district', 'General')})",
                }
                for c in sorted(zero_sales_dues_list, key=lambda x: x['current_dues'], reverse=True)
            ])

            search_roster = st.text_input(
                "🔍 Search within zero-sales debtors:",
                placeholder="Type party name or code...",
                key=f"roster_search_input_{selected_group}"
            )
            if search_roster:
                sq = search_roster.strip().lower()
                df_zero_filtered = df_zero[
                    df_zero["Party Name"].str.lower().str.contains(sq, na=False) |
                    df_zero["Customer Code"].str.lower().str.contains(sq, na=False)
                ]
            else:
                df_zero_filtered = df_zero

            st.dataframe(
                df_zero_filtered,
                use_container_width=True,
                column_config={
                    "Outstanding Dues (Debit ₹)": st.column_config.NumberColumn(format="₹%,.2f"),
                    "Company Payable (Credit ₹)": st.column_config.NumberColumn(format="₹%,.2f"),
                    "Net Due (₹)": st.column_config.NumberColumn(format="₹%,.2f"),
                    "Oldest Due (Days)": st.column_config.NumberColumn(format="%d days"),
                },
                hide_index=True
            )

            # CSV Download button for collection and sales teams
            csv_data = df_zero.to_csv(index=False).encode('utf-8')
            st.download_button(
                "📥 Download Dormant Debtors Collection List (.csv)",
                data=csv_data,
                file_name=f"{selected_group.lower().replace(' ', '_')}_zero_sales_with_dues.csv",
                mime="text/csv",
                key=f"btn_download_zero_sales_dues_{selected_group}"
            )

        st.markdown("---")

    # Section 3: Customer Selector Dropdown
    st.markdown("### 👤 Select Customer for Detailed Intelligence")

    # Determine filtered customer list based on SUNDRY DEBTORS filter criterion
    if is_sundry_debtors and debtor_filter_criterion.startswith("🚨"):
        filtered_customers = zero_sales_dues_list
    elif is_sundry_debtors and debtor_filter_criterion.startswith("🛒"):
        filtered_customers = active_buyers_list
    else:
        filtered_customers = category_customers

    # Further filter by selected sales slab if a specific slab is selected
    if selected_slab and selected_slab != "All Slabs":
        if is_sundry_debtors and debtor_filter_criterion.startswith("🚨"):
            if selected_slab != "0 to 2 Lakh":
                st.info(f"ℹ️ All zero-sales debtors have ₹0.00 purchases and belong to the '0 to 2 Lakh' slab. Displaying all {len(zero_sales_dues_list)} zero-sales debtors.")
        else:
            allowed_codes = bin_label_to_codes.get(selected_slab, set())
            filtered_customers = [c for c in filtered_customers if c['customer_code'] in allowed_codes]

    if not filtered_customers:
        st.warning(f"No customers found matching Category '{selected_group}', Filter '{debtor_filter_criterion}', and Sales Slab '{selected_slab}'.")
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

    # Distinct selectbox key ensuring fresh selection state when switching filter modes
    filter_key_tag = "zero_dues" if (is_sundry_debtors and debtor_filter_criterion.startswith("🚨")) else ("active" if (is_sundry_debtors and debtor_filter_criterion.startswith("🛒")) else "all")
    dropdown_title = f"👤 Select Customer ({len(filtered_customers)} accounts"
    if is_sundry_debtors and debtor_filter_criterion.startswith("🚨"):
        dropdown_title += f" with Sales = 0 & Dues > 0"
    elif is_sundry_debtors and debtor_filter_criterion.startswith("🛒"):
        dropdown_title += f" Active Buyers in {selected_group}"
    else:
        dropdown_title += f" in {selected_group}"

    if selected_slab != "All Slabs" and not (is_sundry_debtors and debtor_filter_criterion.startswith("🚨")):
        dropdown_title += f", Slab: {selected_slab}"
    dropdown_title += "):"

    selected_label = st.selectbox(
        dropdown_title,
        options=cust_options,
        key=f"sales_selected_customer_label_{selected_group}_{filter_key_tag}"
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
        Products this customer **historically purchases** that currently have warehouse batches expiring within **180 days**.
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
        product_totals = summary.get('product_totals', {})

        # Fallback aggregation in case frontend received raw recent_orders without transacted_products
        if not transacted_products and recent_orders:
            p_map = {}
            for r in recent_orders:
                p_code = r.get('product_code', 'UNKNOWN')
                p_name = r.get('product_name') or r.get('description', p_code)
                qty = float(r.get('qty', 0.0) or 0.0)
                free = float(r.get('free', 0.0) or 0.0)
                amt = float(r.get('amount', 0.0) or 0.0)
                rate = float(r.get('rate', 0.0) or 0.0)
                dt = r.get('date', 'N/A')
                if p_code not in p_map:
                    p_map[p_code] = {
                        'product_code': p_code,
                        'product_name': p_name,
                        'description': p_name,
                        'company': 'General',
                        'category': 'Tablets',
                        'total_qty': 0.0,
                        'total_free': 0.0,
                        'total_amount': 0.0,
                        'order_count': 0,
                        'rates': [],
                        'last_purchase_date': dt,
                    }
                entry = p_map[p_code]
                entry['total_qty'] += qty
                entry['total_free'] += free
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
                    'description': p_info['description'],
                    'company': p_info['company'],
                    'category': p_info['category'],
                    'qty': round(p_info['total_qty'], 2),
                    'total_qty': round(p_info['total_qty'], 2),
                    'free': round(p_info['total_free'], 2),
                    'total_free': round(p_info['total_free'], 2),
                    'rate': avg_r,
                    'avg_rate': avg_r,
                    'amount': round(p_info['total_amount'], 2),
                    'total_amount': round(p_info['total_amount'], 2),
                    'order_count': p_info['order_count'],
                    'last_purchase_date': p_info['last_purchase_date'],
                })
            transacted_products.sort(key=lambda x: x['total_amount'], reverse=True)

        if not transacted_products and not recent_orders:
            if current_dues > 0:
                st.warning(
                    f"⚠️ **Dormant Customer Account with Dues**: **{cust_name}** has placed no purchase orders in the uploaded Product-Wise Sales Ledger (Sales = ₹0.00), "
                    f"but currently owes **₹{current_dues:,.2f}** in outstanding debit dues from the Master Customer Ledger."
                )
            else:
                st.info(
                    "ℹ️ **No Product-Wise Customer Ledger Uploaded Yet**: "
                    "The product list transacted by this customer, quantities, rates, and historical sales transactions "
                    "will appear here once the **Product-Wise Customer Ledger** (.xlsx/.xls) is ingested using Uploader 2 above."
                )
        else:
            tot_units = product_totals.get('total_qty', sum(float(p.get('qty', p.get('total_qty', 0.0)) or 0.0) for p in transacted_products))
            tot_free = product_totals.get('total_free', sum(float(p.get('free', p.get('total_free', 0.0)) or 0.0) for p in transacted_products))
            tot_val = product_totals.get('total_amount', sum(float(p.get('amount', p.get('total_amount', 0.0)) or 0.0) for p in transacted_products))

            col_hp1, col_hp2, col_hp3, col_hp4 = st.columns(4)
            with col_hp1:
                st.metric("Unique Products", len(transacted_products))
            with col_hp2:
                st.metric("Total QTY.", f"{tot_units:,.2f}")
            with col_hp3:
                st.metric("Total FREE", f"{tot_free:,.2f}")
            with col_hp4:
                st.metric("Total AMOUNT", f"₹{tot_val:,.2f}")

            st.markdown("---")

            sub_tab_products, sub_tab_txns = st.tabs([
                f"📦 Product-Wise Ledger Table ({len(transacted_products)} Products)",
                f"🧾 Line-by-Line Sales Vouchers ({len(recent_orders)} Records)"
            ])

            with sub_tab_products:
                st.markdown(
                    "**Product-Wise Customer Ledger Table** as extracted from the uploaded Excel:"
                )
                search_query = st.text_input(
                    "🔍 Filter Products:",
                    placeholder="Search by product description / name...",
                    key=f"search_prods_{selected_code}"
                )

                filtered_prods = transacted_products
                if search_query and search_query.strip():
                    sq = search_query.strip().lower()
                    filtered_prods = [
                        p for p in transacted_products
                        if sq in str(p.get('description', p.get('product_name', ''))).lower()
                        or sq in str(p.get('product_code', '')).lower()
                    ]

                if not filtered_prods:
                    st.warning(f"No products found matching '{search_query}'.")
                else:
                    table_rows = []
                    sub_sum_qty = 0.0
                    sub_sum_free = 0.0
                    sub_sum_amt = 0.0

                    for p in filtered_prods:
                        desc_val = str(p.get('description') or p.get('product_name') or '').strip()
                        q_val = float(p.get('qty', p.get('total_qty', 0.0)) or 0.0)
                        f_val = float(p.get('free', p.get('total_free', 0.0)) or 0.0)
                        r_val = float(p.get('rate', p.get('avg_rate', 0.0)) or 0.0)
                        a_val = float(p.get('amount', p.get('total_amount', 0.0)) or 0.0)

                        sub_sum_qty += q_val
                        sub_sum_free += f_val
                        sub_sum_amt += a_val

                        table_rows.append({
                            'D E S C R I P T I O N': desc_val,
                            'QTY.': f"{q_val:,.2f}",
                            'FREE': f"{f_val:,.2f}",
                            'RATE': f"{r_val:,.2f}",
                            'AMOUNT': f"{a_val:,.2f}",
                        })

                    # Total row just like the uploaded Excel
                    table_rows.append({
                        'D E S C R I P T I O N': 'Total',
                        'QTY.': f"{sub_sum_qty:,.2f}",
                        'FREE': f"{sub_sum_free:,.2f}",
                        'RATE': '—',
                        'AMOUNT': f"{sub_sum_amt:,.2f}",
                    })

                    df_purchase_display = pd.DataFrame(table_rows)
                    st.dataframe(df_purchase_display, use_container_width=True, hide_index=True)

                    st.markdown(f"""
                    <div style="background-color: #F8FAFC; border: 1px solid #CBD5E1; border-radius: 6px; padding: 10px 16px; margin-top: 6px; margin-bottom: 12px; display: flex; justify-content: space-between; font-weight: 700; font-size: 0.95rem; color: #1E293B;">
                        <span>📊 Column Totals:</span>
                        <span>QTY.: {sub_sum_qty:,.2f}</span>
                        <span>FREE: {sub_sum_free:,.2f}</span>
                        <span>AMOUNT: ₹{sub_sum_amt:,.2f}</span>
                    </div>
                    """, unsafe_allow_html=True)

                    csv_prods = df_purchase_display.to_csv(index=False).encode('utf-8')
                    st.download_button(
                        label=f"📥 Download Product-Wise Ledger CSV ({cust_name})",
                        data=csv_prods,
                        file_name=f"{selected_code}_product_wise_ledger.csv",
                        mime="text/csv",
                        key=f"btn_dl_prods_{selected_code}"
                    )

            with sub_tab_txns:
                st.markdown("**Line-by-Line Sales Transactions**: Detailed vouchers and invoices.")
                if not recent_orders:
                    st.info("No detailed transaction rows available.")
                else:
                    txn_rows = []
                    t_sum_qty = 0.0
                    t_sum_free = 0.0
                    t_sum_amt = 0.0

                    for r in recent_orders:
                        inv_val = str(r.get('invoice_no') or 'N/A')
                        dt_val = str(r.get('date') or 'N/A')
                        d_val = str(r.get('description') or r.get('product_name') or '').strip()
                        q_val = float(r.get('qty', 0.0) or 0.0)
                        f_val = float(r.get('free', 0.0) or 0.0)
                        r_val = float(r.get('rate', 0.0) or 0.0)
                        a_val = float(r.get('amount', 0.0) or 0.0)

                        t_sum_qty += q_val
                        t_sum_free += f_val
                        t_sum_amt += a_val

                        txn_rows.append({
                            'Invoice / Voucher': inv_val,
                            'Date': dt_val,
                            'D E S C R I P T I O N': d_val,
                            'QTY.': f"{q_val:,.2f}",
                            'FREE': f"{f_val:,.2f}",
                            'RATE': f"{r_val:,.2f}",
                            'AMOUNT': f"{a_val:,.2f}",
                        })

                    txn_rows.append({
                        'Invoice / Voucher': 'Total',
                        'Date': '—',
                        'D E S C R I P T I O N': 'Total',
                        'QTY.': f"{t_sum_qty:,.2f}",
                        'FREE': f"{t_sum_free:,.2f}",
                        'RATE': '—',
                        'AMOUNT': f"{t_sum_amt:,.2f}",
                    })

                    df_txns_display = pd.DataFrame(txn_rows)
                    st.dataframe(df_txns_display, use_container_width=True, hide_index=True)

                    csv_txns = df_txns_display.to_csv(index=False).encode('utf-8')
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
