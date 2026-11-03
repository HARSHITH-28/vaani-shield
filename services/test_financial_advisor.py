
from pprint import pprint

from services.database import get_connection

from services.financial_advisor import (
    compare_monthly_spending,
    calculate_savings_rate,
    detect_unusual_spending,
    generate_budget_recommendations,
    generate_financial_advisory_report,
)


# Automatically identify Arjun
conn = get_connection()

customer = conn.execute("""
    SELECT customer_id
    FROM customers
    WHERE email = ?
""", ("arjun@example.com",)).fetchone()

conn.close()

if customer is None:
    raise ValueError("Arjun's customer record was not found.")

CUSTOMER_ID = customer["customer_id"]

# October 2026 is the latest month in our synthetic dataset.
YEAR = 2026
MONTH = 10


print("\n===== MONTHLY SPENDING COMPARISON =====")
pprint(
    compare_monthly_spending(CUSTOMER_ID, YEAR, MONTH)
)

print("\n===== SAVINGS RATE =====")
pprint(
    calculate_savings_rate(CUSTOMER_ID, YEAR, MONTH)
)

print("\n===== UNUSUAL SPENDING DETECTION =====")
pprint(
    detect_unusual_spending(CUSTOMER_ID, YEAR, MONTH)
)

print("\n===== BUDGET RECOMMENDATIONS =====")
pprint(
    generate_budget_recommendations(
        CUSTOMER_ID, YEAR, MONTH
    )
)

print("\n===== COMPLETE FINANCIAL ADVISORY REPORT =====")
pprint(
    generate_financial_advisory_report(
        CUSTOMER_ID, YEAR, MONTH
    )
)
