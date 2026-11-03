
from datetime import datetime

from services.database import get_connection

from services.banking_service import (
    get_customer_profile,
    get_account_balance,
    get_recent_transactions,
    get_card_details,
    get_loan_details,
)

from services.financial_intelligence import (
    get_financial_insights,
)

from services.financial_advisor import (
    generate_financial_advisory_report,
)


def get_latest_transaction_period(customer_id):
    """Find the latest transaction year and month for a customer."""

    conn = get_connection()

    row = conn.execute("""
        SELECT MAX(t.transaction_date) AS latest_date
        FROM transactions t
        JOIN accounts a ON t.account_id = a.account_id
        WHERE a.customer_id = ?
    """, (customer_id,)).fetchone()

    conn.close()

    if row is None or row["latest_date"] is None:
        today = datetime.now()
        return today.year, today.month

    latest_date = datetime.strptime(
        row["latest_date"][:10],
        "%Y-%m-%d"
    )

    return latest_date.year, latest_date.month


def get_customer_context(customer_id, year=None, month=None):
    """
    Aggregate customer, banking and financial intelligence
    into one structured, AI-ready context object.
    """

    profile = get_customer_profile(customer_id)

    if profile is None:
        raise ValueError("Customer not found.")

    if year is None or month is None:
        latest_year, latest_month = (
            get_latest_transaction_period(customer_id)
        )

        year = year if year is not None else latest_year
        month = month if month is not None else latest_month

    if not 1 <= month <= 12:
        raise ValueError("Month must be between 1 and 12.")

    account_balance = get_account_balance(customer_id)

    recent_transactions = get_recent_transactions(
        customer_id,
        limit=10
    )

    cards = get_card_details(customer_id)

    loans = get_loan_details(customer_id)

    financial_insights = get_financial_insights(
        customer_id
    )

    advisory_report = generate_financial_advisory_report(
        customer_id,
        year,
        month
    )

    return {
        "customer": profile,

        "banking": {
            "account_balance": account_balance,
            "recent_transactions": recent_transactions,
            "cards": cards,
            "loans": loans,
        },

        "financial_intelligence": {
            "summary": financial_insights["summary"],
            "insights": financial_insights["insights"],
        },

        "financial_advisory": advisory_report,

        "metadata": {
            "customer_id": customer_id,
            "report_period": f"{year}-{month:02d}",
            "data_source": "VaaniShield synthetic banking database",
            "is_synthetic_data": True,
        },
    }


if __name__ == "__main__":
    conn = get_connection()

    customer = conn.execute("""
        SELECT customer_id
        FROM customers
        WHERE email = ?
    """, ("arjun@example.com",)).fetchone()

    conn.close()

    if customer is None:
        raise ValueError("Arjun's customer record was not found.")

    context = get_customer_context(
        customer["customer_id"]
    )

    import json

    print(json.dumps(context, indent=4, default=str))
