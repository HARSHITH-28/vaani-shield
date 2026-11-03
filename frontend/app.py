
import streamlit as st
import requests
import plotly.express as px
import pandas as pd

# --------------------------------------------------
# CONFIGURATION
# --------------------------------------------------

API_BASE = "http://127.0.0.1:8000"

st.set_page_config(
    page_title="VaaniShield | Banking",
    page_icon="🛡️",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=DM+Sans:wght@400;500;600;700;800&family=Space+Grotesk:wght@500;600;700&display=swap');
:root { --bg:#080d19; --line:rgba(148,163,184,.16); --muted:#91a2ba; }
.stApp { background:radial-gradient(ellipse at 12% 0%,rgba(24,112,122,.17),transparent 34%),radial-gradient(ellipse at 90% 8%,rgba(64,91,177,.16),transparent 30%),#080d19; color:#edf4ff; font-family:'DM Sans',sans-serif; }
[data-testid="stHeader"] { background:rgba(8,13,25,.75); }
[data-testid="stSidebar"] {
    background:
        radial-gradient(ellipse at 15% 0%, rgba(30, 174, 160, .13), transparent 38%),
        linear-gradient(180deg, #0e1929 0%, #0a1220 52%, #080f1b 100%);
    border-right: 1px solid rgba(119, 160, 190, .15);
}
[data-testid="stSidebar"] > div { padding:1.15rem .85rem 1.5rem; }
[data-testid="stSidebar"] [data-testid="stVerticalBlock"] { gap:.65rem; }
[data-testid="stSidebar"] [data-testid="stMarkdownContainer"] p {
    color:#91a6bd;
}
[data-testid="stSidebar"] [data-testid="stSelectbox"] label,
[data-testid="stSidebar"] [data-testid="stRadio"] > label {
    color:#9fb2c8 !important;
    font-size:.72rem !important;
    font-weight:800 !important;
    letter-spacing:1.35px !important;
    text-transform:uppercase !important;
}
[data-testid="stSidebar"] [data-testid="stSelectbox"] div[data-baseweb="select"] > div {
    background:rgba(19, 34, 53, .92) !important;
    border:1px solid rgba(118, 157, 187, .2) !important;
    border-radius:12px !important;
    min-height:44px;
}
[data-testid="stSidebar"] [data-testid="stRadio"] [role="radiogroup"] {
    gap:5px;
}
[data-testid="stSidebar"] [data-testid="stRadio"] [role="radiogroup"] > label {
    background:transparent;
    border:1px solid transparent;
    border-radius:11px;
    padding:10px 12px;
    margin:0;
    transition:all .18s ease;
}
[data-testid="stSidebar"] [data-testid="stRadio"] [role="radiogroup"] > label:hover {
    background:rgba(42, 70, 91, .38);
    border-color:rgba(105, 159, 180, .15);
}
[data-testid="stSidebar"] [data-testid="stRadio"] [role="radiogroup"] > label:has(input:checked) {
    background:linear-gradient(100deg, rgba(21, 130, 123, .25), rgba(26, 91, 120, .18));
    border:1px solid rgba(69, 207, 187, .28);
    box-shadow:inset 3px 0 0 #45d2bd, 0 5px 18px rgba(0,0,0,.12);
}
[data-testid="stSidebar"] [data-testid="stRadio"] [role="radiogroup"] > label p {
    color:#dce8f5 !important;
    font-size:.91rem;
    font-weight:600;
}
[data-testid="stSidebar"] [data-testid="stRadio"] [role="radiogroup"] > label:has(input:checked) p {
    color:#8cf0df !important;
    font-weight:750;
}
[data-testid="stSidebar"] [data-testid="stRadio"] input {
    accent-color:#42d2bd;
}
[data-testid="stSidebar"] hr {
    border-color:rgba(137, 170, 198, .13);
    margin:.55rem 0;
}
.sidebar-brand {
    display:flex;
    align-items:center;
    gap:12px;
    padding:13px 11px 16px;
    margin:0 0 7px;
}
.sidebar-brand-mark {
    width:43px;height:43px;flex:0 0 43px;
    display:flex;align-items:center;justify-content:center;
    border-radius:14px;
    color:#9af5e6;
    font-size:22px;
    background:linear-gradient(145deg,rgba(28,160,148,.28),rgba(39,92,139,.28));
    border:1px solid rgba(91,224,204,.3);
    box-shadow:0 8px 24px rgba(0,0,0,.2), inset 0 1px rgba(255,255,255,.08);
}
.sidebar-brand-name {
    color:#f2f8ff;font:700 19px 'Space Grotesk',sans-serif;
    letter-spacing:-.5px;line-height:1.15;
}
.sidebar-brand-tag {
    color:#7f9ab2;font-size:9px;font-weight:800;
    letter-spacing:1.55px;text-transform:uppercase;margin-top:5px;
}
.sidebar-section {
    color:#64849e;font-size:9px;font-weight:800;
    letter-spacing:1.8px;text-transform:uppercase;
    padding:4px 11px 0;
}
.sidebar-footer {
    border:1px solid rgba(113,157,184,.14);
    background:linear-gradient(135deg,rgba(21,43,62,.7),rgba(13,27,43,.65));
    border-radius:13px;padding:12px 13px;margin-top:9px;
}
.sidebar-footer-title {
    color:#c8d9e9;font-size:11px;font-weight:750;letter-spacing:.3px;
}
.sidebar-footer-copy {
    color:#718ba4;font-size:10px;line-height:1.55;margin-top:4px;
}
h1,h2,h3 { font-family:'Space Grotesk',sans-serif !important; letter-spacing:-.45px; color:#f4f8ff; }
.main .block-container { padding-top:2.1rem; padding-bottom:3rem; max-width:1500px; }
div[data-testid="stMetric"] { background:linear-gradient(145deg,rgba(23,39,61,.96),rgba(15,27,44,.96)); border:1px solid var(--line); border-radius:17px; padding:18px 20px; box-shadow:0 10px 30px rgba(0,0,0,.12); }
div[data-testid="stMetricLabel"] { color:#9eb0c8; font-weight:600; }
div[data-testid="stMetricValue"] { color:#f4f8ff; font-family:'Space Grotesk',sans-serif; }
[data-testid="stDataFrame"],[data-testid="stTable"] { border:1px solid var(--line); border-radius:14px; overflow:hidden; }
[data-testid="stTextInput"] input,[data-testid="stNumberInput"] input,[data-testid="stDateInput"] input,[data-testid="stSelectbox"] div[data-baseweb="select"] > div { background:#111e31 !important; border-color:#2b405b !important; border-radius:10px !important; color:#edf4ff !important; }
.stButton > button,[data-testid="stFormSubmitButton"] button { border-radius:10px; border:1px solid rgba(53,214,189,.45); background:linear-gradient(110deg,#137f7a,#176b88); color:white; font-weight:700; padding:.55rem 1.1rem; }
hr { border-color:var(--line); }
.hero { padding:25px 28px; border:1px solid rgba(91,211,201,.23); border-radius:22px; background:linear-gradient(115deg,rgba(16,54,66,.92),rgba(20,35,62,.94) 60%,rgba(30,39,75,.9)); box-shadow:0 18px 45px rgba(0,0,0,.18); margin-bottom:22px; }
.hero-kicker { color:#65e6d2; font-size:11px; font-weight:800; letter-spacing:2px; text-transform:uppercase; }
.hero-title { color:#f6fbff; font:700 29px 'Space Grotesk',sans-serif; margin:7px 0 4px; }
.hero-subtitle { color:#b7c9dc; font-size:14px; }
.section-eyebrow { color:#6edcca; font-size:11px; font-weight:800; letter-spacing:1.6px; text-transform:uppercase; margin-bottom:5px; }
.security-banner { background:linear-gradient(110deg,rgba(13,74,66,.45),rgba(15,39,57,.8)); border:1px solid rgba(53,214,189,.35); border-radius:14px; padding:17px 20px; color:#a8f4e4; }
.caption-muted { color:var(--muted); font-size:12px; }
#MainMenu,footer { visibility:hidden; }
</style>
""", unsafe_allow_html=True)


# --------------------------------------------------
# API HELPERS
# --------------------------------------------------

def api_get(endpoint, params=None):
    try:
        response = requests.get(
            f"{API_BASE}{endpoint}",
            params=params,
            timeout=10,
        )
        response.raise_for_status()
        return response.json()

    except requests.exceptions.RequestException as error:
        st.error(f"Backend connection error: {error}")
        st.info("Make sure the FastAPI backend is running.")
        st.stop()


def normalize_collection(data):
    """Handle both list and dictionary API responses."""
    if isinstance(data, list):
        return data

    if isinstance(data, dict):
        if "value" in data:
            return data["value"]

    return []


def money(value):
    return f"₹{value:,.2f}"


# --------------------------------------------------
# SIDEBAR
# --------------------------------------------------

st.sidebar.markdown(
    '''
    <div class="sidebar-brand">
        <div class="sidebar-brand-mark">⛨</div>
        <div>
            <div class="sidebar-brand-name">VaaniShield</div>
            <div class="sidebar-brand-tag">Intelligent Banking</div>
        </div>
    </div>
    <div class="sidebar-section">Workspace</div>
    ''',
    unsafe_allow_html=True,
)

customer_options = {
    "Arjun Sharma": 4,
    "Priya Nair": 5,
    "Rahul Verma": 6,
}

selected_customer = st.sidebar.selectbox(
    "DEMO CUSTOMER",
    list(customer_options.keys()),
)

customer_id = customer_options[selected_customer]

st.sidebar.markdown('<div class="sidebar-section">Your workspace</div>', unsafe_allow_html=True)

page = st.sidebar.radio(
    "NAVIGATION",
    [
        "Overview",
        "Transactions",
        "Financial Intelligence",
        "AI Financial Advisor",
        "Cards & Loans",
        "Security Centre",
    ],
    label_visibility="collapsed",
)

st.sidebar.markdown(
    '''
    <div class="sidebar-footer">
        <div class="sidebar-footer-title">✦ VaaniShield 2.0</div>
        <div class="sidebar-footer-copy">
            Secure financial command centre<br>
            Synthetic demonstration environment
        </div>
    </div>
    ''',
    unsafe_allow_html=True,
)


# --------------------------------------------------
# LOAD CUSTOMER DATA
# --------------------------------------------------

context = api_get(
    f"/api/customers/{customer_id}/context"
)

customer = context["customer"]
banking = context["banking"]
financial = context["financial_intelligence"]
advisory = context["financial_advisory"]

accounts = normalize_collection(
    banking["account_balance"]
)

transactions = normalize_collection(
    banking["recent_transactions"]
)

cards = normalize_collection(
    banking["cards"]
)

loans = normalize_collection(
    banking["loans"]
)

balance = sum(
    account.get("balance", 0)
    for account in accounts
)

summary = financial["summary"]

# --------------------------------------------------
# HEADER
# --------------------------------------------------

st.markdown(
    f"""
    <div class="hero">
        <div class="hero-kicker">VAANISHIELD 2.0 · SECURE BANKING EXPERIENCE</div>
        <div class="hero-title">Good to see you, {customer["name"].split()[0]}.</div>
        <div class="hero-subtitle">Your financial command centre — insights, transactions and account security in one place.</div>
    </div>
    """,
    unsafe_allow_html=True,
)

st.write("")


# --------------------------------------------------
# OVERVIEW PAGE
# --------------------------------------------------

if page == "Overview":

    st.markdown(
        '<div class="section-title">Banking Overview</div>',
        unsafe_allow_html=True,
    )

    col1, col2, col3, col4 = st.columns(4)

    col1.metric(
        "Total Balance",
        money(balance),
    )

    col2.metric(
        "Monthly Income",
        money(customer["monthly_income"]),
    )

    col3.metric(
        "Total Spending",
        money(summary["total_spent"]),
    )

    col4.metric(
        "Estimated Savings",
        money(advisory["savings_analysis"]["estimated_savings"]),
    )

    st.write("")

    left, right = st.columns([1.5, 1])

    with left:
        st.markdown("### Recent Transactions")

        if transactions:
            transaction_df = pd.DataFrame(transactions)

            display_columns = [
                "transaction_date",
                "merchant",
                "category",
                "transaction_type",
                "amount",
                "status",
            ]

            st.dataframe(
                transaction_df[display_columns],
                use_container_width=True,
                hide_index=True,
            )
        else:
            st.info("No transactions available.")

    with right:
        st.markdown("### Account Details")

        for account in accounts:
            st.markdown(f"""
            **{account['account_type']} Account**

            Account: `{account['account_number']}`

            Balance: **{money(account['balance'])}**

            Status: {account['status']}
            """)
            st.divider()

        st.markdown("### Financial Health")

        savings_rate = advisory["savings_analysis"]["savings_rate"]

        st.metric(
            "Estimated Savings Rate",
            f"{savings_rate:.2f}%",
        )

        st.progress(
            min(max(savings_rate / 100, 0), 1)
        )


# --------------------------------------------------
# TRANSACTIONS PAGE
# --------------------------------------------------

elif page == "Transactions":

    st.markdown('<div class="section-eyebrow">BANKING INTELLIGENCE</div>', unsafe_allow_html=True)
    st.markdown("## Transaction Explorer")
    st.markdown(
        '<div class="caption-muted">Search and narrow down your transaction history with flexible filters.</div>',
        unsafe_allow_html=True,
    )

    all_transactions = normalize_collection(
        api_get(f"/api/customers/{customer_id}/transactions?limit=100")
    )

    if all_transactions:
        base_df = pd.DataFrame(all_transactions)
        for column in ["merchant", "category", "transaction_type", "transaction_date"]:
            if column not in base_df.columns:
                base_df[column] = ""

        with st.container(border=True):
            st.markdown("#### Find a transaction")
            with st.form("transaction_search_form"):
                f1, f2, f3 = st.columns([1.5, 1, 1])
                with f1:
                    merchant = st.text_input("Merchant contains", placeholder="e.g. DMart, Amazon")
                with f2:
                    categories = sorted(str(value) for value in base_df["category"].dropna().unique() if str(value).strip())
                    category = st.selectbox("Category", ["All categories"] + categories)
                with f3:
                    transaction_type = st.selectbox("Transaction type", ["All types", "DEBIT", "CREDIT"])

                f4, f5, f6, f7 = st.columns(4)
                with f4:
                    min_amount = st.number_input("Minimum amount (₹)", min_value=0.0, value=0.0, step=500.0)
                with f5:
                    max_amount = st.number_input("Maximum amount (₹)", min_value=0.0, value=0.0, step=500.0,
                                                 help="Keep at 0 for no upper limit.")
                with f6:
                    start_date = st.date_input("From date", value=None)
                with f7:
                    end_date = st.date_input("To date", value=None)

                submitted = st.form_submit_button("Apply filters", use_container_width=True)

        if submitted:
            if max_amount > 0 and min_amount > max_amount:
                st.error("Maximum amount must be greater than or equal to minimum amount.")
                filtered_df = pd.DataFrame()
            elif start_date and end_date and start_date > end_date:
                st.error("The start date cannot be after the end date.")
                filtered_df = pd.DataFrame()
            else:
                params = {}
                if merchant.strip():
                    params["merchant"] = merchant.strip()
                if category != "All categories":
                    params["category"] = category
                if transaction_type != "All types":
                    params["transaction_type"] = transaction_type
                if min_amount > 0:
                    params["min_amount"] = min_amount
                if max_amount > 0:
                    params["max_amount"] = max_amount
                if start_date:
                    params["start_date"] = start_date.isoformat()
                if end_date:
                    params["end_date"] = end_date.isoformat()

                filtered_transactions = normalize_collection(
                    api_get(f"/api/customers/{customer_id}/transactions/search", params=params)
                )
                filtered_df = pd.DataFrame(filtered_transactions)
        else:
            filtered_df = base_df.copy()

        st.write("")
        total_count = len(filtered_df)
        debit_total = 0.0
        credit_total = 0.0
        if total_count and "amount" in filtered_df.columns and "transaction_type" in filtered_df.columns:
            amounts = pd.to_numeric(filtered_df["amount"], errors="coerce").fillna(0)
            debit_total = amounts[filtered_df["transaction_type"] == "DEBIT"].sum()
            credit_total = amounts[filtered_df["transaction_type"] == "CREDIT"].sum()

        m1, m2, m3 = st.columns(3)
        m1.metric("Matching transactions", f"{total_count}")
        m2.metric("Total debits", money(float(debit_total)))
        m3.metric("Total credits", money(float(credit_total)))

        st.markdown("### Results")
        if total_count:
            preferred = ["transaction_date", "merchant", "category", "transaction_type", "amount", "status", "reference_id"]
            display_columns = [column for column in preferred if column in filtered_df.columns]
            display_df = filtered_df[display_columns].copy()
            if "amount" in display_df.columns:
                display_df["amount"] = display_df["amount"].apply(money)
            if "transaction_type" in display_df.columns:
                display_df["transaction_type"] = display_df["transaction_type"].replace({"DEBIT": "↓ DEBIT", "CREDIT": "↑ CREDIT"})
            st.dataframe(display_df, use_container_width=True, hide_index=True)
        else:
            st.info("No transactions match these filters. Try widening your search.")

        st.caption("Transaction records are synthetic demonstration data.")
    else:
        st.info("No transactions found.")


# --------------------------------------------------
# FINANCIAL INTELLIGENCE PAGE
# --------------------------------------------------

elif page == "Financial Intelligence":

    st.markdown("## Financial Intelligence")

    col1, col2, col3 = st.columns(3)

    col1.metric(
        "Total Credits",
        money(summary["total_credited"]),
    )

    col2.metric(
        "Total Debits",
        money(summary["total_spent"]),
    )

    col3.metric(
        "Net Cash Flow",
        money(summary["net_cash_flow"]),
    )

    st.write("")

    categories = normalize_collection(
        api_get(
            f"/api/customers/{customer_id}/financial/categories"
        )
    )

    if categories:
        category_df = pd.DataFrame(categories)

        st.markdown("### Spending by Category")

        # Adapt to the actual fields returned by the service.
        numeric_columns = [
            column for column in category_df.columns
            if pd.api.types.is_numeric_dtype(category_df[column])
        ]

        if numeric_columns:
            label_candidates = [
                column for column in category_df.columns
                if column not in numeric_columns
            ]

            if label_candidates:
                fig = px.pie(
                    category_df,
                    names=label_candidates[0],
                    values=numeric_columns[0],
                    hole=0.55,
                    title="Category-wise Spending",
                )

                fig.update_layout(
                    template="plotly_dark",
                    paper_bgcolor="#0b1120",
                    plot_bgcolor="#0b1120",
                )

                st.plotly_chart(
                    fig,
                    use_container_width=True,
                )
            else:
                st.dataframe(category_df)
        else:
            st.dataframe(category_df)
    else:
        st.info("Category spending data is not available.")

    st.markdown("### Financial Insights")

    for insight in financial["insights"]:
        st.info(insight.get("message", str(insight)))


# --------------------------------------------------
# AI FINANCIAL ADVISOR PAGE
# --------------------------------------------------

elif page == "AI Financial Advisor":

    st.markdown("## AI Financial Advisor")

    savings = advisory["savings_analysis"]
    comparison = advisory["spending_comparison"]
    budget = advisory["budget_analysis"]

    c1, c2, c3 = st.columns(3)

    c1.metric(
        "Savings Rate",
        f"{savings['savings_rate']:.2f}%",
    )

    c2.metric(
        "Monthly Spending",
        money(savings["monthly_spending"]),
    )

    c3.metric(
        "Estimated Savings",
        money(savings["estimated_savings"]),
    )

    st.markdown("### Monthly Spending Comparison")

    st.write(
        f"Current month: **{comparison['current_month']}**"
    )

    st.write(
        f"Previous month: **{comparison['previous_month']}**"
    )

    st.metric(
        "Spending Change",
        money(comparison["difference"]),
        delta=f"{comparison['percentage_change']}%",
        delta_color="inverse",
    )

    st.markdown("### Budget Analysis")

    budget_columns = st.columns(3)

    budget_columns[0].metric(
        "Needs",
        money(budget["needs_spending"]),
    )

    budget_columns[1].metric(
        "Wants",
        money(budget["wants_spending"]),
    )

    budget_columns[2].metric(
        "Savings",
        money(budget["estimated_savings"]),
    )

    st.markdown("### Unusual Spending")

    unusual = advisory["unusual_spending"]

    if unusual:
        st.dataframe(
            pd.DataFrame(unusual),
            use_container_width=True,
            hide_index=True,
        )
        st.caption(
            "These are statistical spending alerts, not confirmed fraud."
        )
    else:
        st.success("No unusual spending patterns detected.")

    st.markdown("### Recommendations")

    for recommendation in budget["recommendations"]:
        st.info(recommendation)


# --------------------------------------------------
# CARDS AND LOANS PAGE
# --------------------------------------------------

elif page == "Cards & Loans":

    st.markdown("## Cards & Loans")

    st.markdown("### Your Cards")

    if cards:
        for card in cards:
            st.markdown(f"""
            **{card['card_type']} CARD**

            •••• •••• •••• {card['last_four']}

            Expiry: {card['expiry_date']}

            Status: {card['status']}
            """)
            st.divider()
    else:
        st.info("No cards found.")

    st.markdown("### Your Loans")

    if loans:
        for loan in loans:
            st.markdown(f"""
            **{loan['loan_type']} LOAN**

            Principal: {money(loan['principal_amount'])}

            Outstanding: {money(loan['outstanding_amount'])}

            Interest Rate: {loan['interest_rate']}%

            Tenure: {loan['tenure_months']} months

            Status: {loan['status']}
            """)
            st.divider()
    else:
        st.info("No loans found.")


# --------------------------------------------------
# SECURITY CENTRE PAGE
# --------------------------------------------------

elif page == "Security Centre":

    st.markdown("## Security Centre")

    st.markdown("""
    <div class="security-banner">
        🛡️ <b>VaaniShield Security Layer</b><br>
        Banking data is currently running in a synthetic
        demonstration environment.
    </div>
    """, unsafe_allow_html=True)

    st.write("")

    st.markdown("### Security Capabilities")

    st.info(
        "Scam detection: Agent-based detection workflow is planned."
    )

    st.info(
        "Voice identity verification: Biometrics integration pending."
    )

    st.info(
        "Fraud investigation: Investigation dashboard is planned."
    )

    st.info(
        "Conversation intelligence: Gnani Analytics integration pending."
    )

    st.warning(
        "No real-time fraud monitoring or real banking protection "
        "is currently active."
    )

st.divider()

st.caption(
    "VaaniShield 2.0 | AI-powered banking security | "
    "Synthetic demonstration environment"
)
