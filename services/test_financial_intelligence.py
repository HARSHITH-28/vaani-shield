
from pprint import pprint

from services.financial_intelligence import (
    get_spending_summary,
    get_category_breakdown,
    get_top_merchants,
    get_monthly_spending,
    find_large_transactions,
    get_financial_insights
)

CUSTOMER_ID = 4

print("\n===== SPENDING SUMMARY =====")
pprint(get_spending_summary(CUSTOMER_ID))

print("\n===== CATEGORY BREAKDOWN =====")
pprint(get_category_breakdown(CUSTOMER_ID))

print("\n===== TOP MERCHANTS =====")
pprint(get_top_merchants(CUSTOMER_ID))

print("\n===== MONTHLY SPENDING =====")
pprint(get_monthly_spending(CUSTOMER_ID, 2026, 10))

print("\n===== LARGE TRANSACTIONS =====")
pprint(find_large_transactions(CUSTOMER_ID, 10000))

print("\n===== FINANCIAL INSIGHTS =====")
pprint(get_financial_insights(CUSTOMER_ID))
