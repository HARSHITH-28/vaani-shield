
from pprint import pprint
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from services.banking_service import get_customer_profile_by_key
from services.database import initialize_database
from services.agent_tools import (
    execute_tool,
    get_available_tools,
)
from services.principal import CustomerPrincipal


initialize_database()
customer = get_customer_profile_by_key("demo-arjun")

if customer is None:
    raise ValueError("Arjun's customer record was not found.")

now = datetime.now(timezone.utc)
PRINCIPAL = CustomerPrincipal(
    customer_key=customer["customer_key"],
    session_id=uuid4(),
    issued_at=now,
    expires_at=now + timedelta(hours=1),
)


def run_test(name, tool_name, arguments=None):
    print(f"\n===== {name} =====")

    try:
        result = execute_tool(
            tool_name,
            PRINCIPAL,
            arguments or {}
        )
        pprint(result)

    except Exception as error:
        print(f"ERROR: {error}")


print("\n===== AVAILABLE AGENT TOOLS =====")
for tool in get_available_tools():
    print("-", tool["name"])


run_test(
    "CUSTOMER PROFILE",
    "get_customer_profile"
)

run_test(
    "ACCOUNT BALANCE",
    "get_account_balance"
)

run_test(
    "RECENT TRANSACTIONS",
    "get_recent_transactions",
    {"limit": 5}
)

run_test(
    "TRANSACTION SEARCH",
    "search_transactions",
    {"category": "GROCERIES", "min_amount": 500}
)

run_test(
    "CARD DETAILS",
    "get_card_details"
)

run_test(
    "LOAN DETAILS",
    "get_loan_details"
)

run_test(
    "EMI CALCULATION",
    "calculate_emi",
    {
        "principal": 500000,
        "annual_interest_rate": 9.5,
        "tenure_months": 60
    }
)

run_test(
    "SPENDING SUMMARY",
    "get_spending_summary"
)

run_test(
    "MONTHLY COMPARISON",
    "compare_monthly_spending",
    {"year": 2026, "month": 10}
)

run_test(
    "UNUSUAL SPENDING",
    "detect_unusual_spending",
    {"year": 2026, "month": 10}
)

run_test(
    "COMPLETE CUSTOMER CONTEXT",
    "get_customer_context"
)


# Security tests
print("\n===== SECURITY TESTS =====")

run_test(
    "UNKNOWN TOOL REJECTION",
    "transfer_money",
    {"amount": 1000}
)

run_test(
    "INVALID LIMIT REJECTION",
    "get_recent_transactions",
    {"limit": 1000}
)

run_test(
    "UNEXPECTED ARGUMENT REJECTION",
    "get_account_balance",
    {"customer_id": 5}
)

print("\n===== AGENT TOOL TESTING COMPLETED =====")
