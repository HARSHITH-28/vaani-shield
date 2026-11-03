
"""
VaaniShield Agent Tool Router

Provides controlled access to read-only banking and
financial intelligence services.

The LLM cannot directly access or modify the database.
"""

from services.banking_service import (
    get_customer_profile,
    get_account_balance,
    get_recent_transactions,
    search_transactions,
    get_card_details,
    get_loan_details,
    calculate_emi,
)

from services.financial_intelligence import (
    get_spending_summary,
    get_category_breakdown,
    get_top_merchants,
    find_large_transactions,
)

from services.financial_advisor import (
    compare_monthly_spending,
    calculate_savings_rate,
    detect_unusual_spending,
    generate_budget_recommendations,
    generate_financial_advisory_report,
)

from services.customer_context import get_customer_context


# --------------------------------------------------
# TOOL DEFINITIONS
# --------------------------------------------------

TOOL_DEFINITIONS = [
    {
        "name": "get_customer_profile",
        "description": "Retrieve the authenticated customer's profile."
    },
    {
        "name": "get_account_balance",
        "description": "Retrieve balances of the customer's accounts."
    },
    {
        "name": "get_recent_transactions",
        "description": "Retrieve recent account transactions.",
        "parameters": {
            "limit": "integer, optional"
        }
    },
    {
        "name": "search_transactions",
        "description": "Search transactions by category and minimum amount.",
        "parameters": {
            "category": "string, optional",
            "min_amount": "number, optional"
        }
    },
    {
        "name": "get_card_details",
        "description": "Retrieve the customer's card details."
    },
    {
        "name": "get_loan_details",
        "description": "Retrieve the customer's loan details."
    },
    {
        "name": "calculate_emi",
        "description": "Calculate the EMI for a hypothetical loan.",
        "parameters": {
            "principal": "number",
            "annual_interest_rate": "number",
            "tenure_months": "integer"
        }
    },
    {
        "name": "get_spending_summary",
        "description": "Retrieve total credits, debits and net cash flow."
    },
    {
        "name": "get_category_breakdown",
        "description": "Retrieve spending grouped by category."
    },
    {
        "name": "get_top_merchants",
        "description": "Retrieve merchants with the highest spending.",
        "parameters": {
            "limit": "integer, optional"
        }
    },
    {
        "name": "find_large_transactions",
        "description": "Find large successful debit transactions.",
        "parameters": {
            "threshold": "number, optional"
        }
    },
    {
        "name": "compare_monthly_spending",
        "description": "Compare spending with the previous month.",
        "parameters": {
            "year": "integer",
            "month": "integer"
        }
    },
    {
        "name": "calculate_savings_rate",
        "description": "Calculate estimated savings and savings rate.",
        "parameters": {
            "year": "integer",
            "month": "integer"
        }
    },
    {
        "name": "detect_unusual_spending",
        "description": "Identify unusual category-level spending increases.",
        "parameters": {
            "year": "integer",
            "month": "integer"
        }
    },
    {
        "name": "generate_budget_recommendations",
        "description": "Generate illustrative budget recommendations.",
        "parameters": {
            "year": "integer",
            "month": "integer"
        }
    },
    {
        "name": "generate_financial_advisory_report",
        "description": "Generate a complete financial advisory report.",
        "parameters": {
            "year": "integer",
            "month": "integer"
        }
    },
    {
        "name": "get_customer_context",
        "description": "Retrieve consolidated banking and financial context.",
        "parameters": {
            "year": "integer, optional",
            "month": "integer, optional"
        }
    }
]


# --------------------------------------------------
# ARGUMENT VALIDATION
# --------------------------------------------------

def validate_month(year, month):
    if not isinstance(year, int) or not 2000 <= year <= 2100:
        raise ValueError("Invalid year.")

    if not isinstance(month, int) or not 1 <= month <= 12:
        raise ValueError("Month must be between 1 and 12.")


def validate_arguments(arguments, allowed, required=None):
    required = required or []

    if not isinstance(arguments, dict):
        raise ValueError("Tool arguments must be a dictionary.")

    unknown = set(arguments) - set(allowed)

    if unknown:
        raise ValueError(
            f"Unsupported arguments: {sorted(unknown)}"
        )

    missing = set(required) - set(arguments)

    if missing:
        raise ValueError(
            f"Missing required arguments: {sorted(missing)}"
        )


# --------------------------------------------------
# TOOL EXECUTION
# --------------------------------------------------

def execute_tool(tool_name, customer_id, arguments=None):
    """
    Execute an approved read-only banking tool.

    customer_id must come from the authenticated session,
    never from LLM-generated arguments.
    """

    if not isinstance(customer_id, int) or customer_id <= 0:
        raise ValueError("Invalid authenticated customer ID.")

    arguments = arguments or {}

    if not isinstance(arguments, dict):
        raise ValueError("Arguments must be a dictionary.")

    # Customer profile
    if tool_name == "get_customer_profile":
        validate_arguments(arguments, [])
        return get_customer_profile(customer_id)

    # Account balance
    elif tool_name == "get_account_balance":
        validate_arguments(arguments, [])
        return get_account_balance(customer_id)

    # Recent transactions
    elif tool_name == "get_recent_transactions":
        validate_arguments(arguments, ["limit"])
        limit = arguments.get("limit", 10)

        if not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError("Limit must be between 1 and 100.")

        return get_recent_transactions(customer_id, limit)

    # Transaction search
    elif tool_name == "search_transactions":
        validate_arguments(
            arguments,
            ["category", "min_amount"]
        )

        category = arguments.get("category")
        min_amount = arguments.get("min_amount", 0)

        if category is not None and not isinstance(category, str):
            raise ValueError("Category must be a string.")

        if (
            isinstance(min_amount, bool)
            or not isinstance(min_amount, (int, float))
            or min_amount < 0
        ):
            raise ValueError("Minimum amount must be non-negative.")

        return search_transactions(
            customer_id,
            category=category,
            min_amount=min_amount
        )

    # Cards
    elif tool_name == "get_card_details":
        validate_arguments(arguments, [])
        return get_card_details(customer_id)

    # Loans
    elif tool_name == "get_loan_details":
        validate_arguments(arguments, [])
        return get_loan_details(customer_id)

    # EMI calculation
    elif tool_name == "calculate_emi":
        validate_arguments(
            arguments,
            [
                "principal",
                "annual_interest_rate",
                "tenure_months"
            ],
            required=[
                "principal",
                "annual_interest_rate",
                "tenure_months"
            ]
        )

        return calculate_emi(
            arguments["principal"],
            arguments["annual_interest_rate"],
            arguments["tenure_months"]
        )

    # Spending summary
    elif tool_name == "get_spending_summary":
        validate_arguments(arguments, [])
        return get_spending_summary(customer_id)

    # Category breakdown
    elif tool_name == "get_category_breakdown":
        validate_arguments(arguments, [])
        return get_category_breakdown(customer_id)

    # Top merchants
    elif tool_name == "get_top_merchants":
        validate_arguments(arguments, ["limit"])
        limit = arguments.get("limit", 5)

        if not isinstance(limit, int) or not 1 <= limit <= 50:
            raise ValueError("Limit must be between 1 and 50.")

        return get_top_merchants(customer_id, limit)

    # Large transactions
    elif tool_name == "find_large_transactions":
        validate_arguments(arguments, ["threshold"])
        threshold = arguments.get("threshold", 10000)

        if (
            isinstance(threshold, bool)
            or not isinstance(threshold, (int, float))
            or threshold < 0
        ):
            raise ValueError("Threshold must be non-negative.")

        return find_large_transactions(
            customer_id,
            threshold
        )

    # Monthly spending comparison
    elif tool_name == "compare_monthly_spending":
        validate_arguments(
            arguments,
            ["year", "month"],
            ["year", "month"]
        )

        validate_month(arguments["year"], arguments["month"])

        return compare_monthly_spending(
            customer_id,
            arguments["year"],
            arguments["month"]
        )

    # Savings analysis
    elif tool_name == "calculate_savings_rate":
        validate_arguments(
            arguments,
            ["year", "month"],
            ["year", "month"]
        )

        validate_month(arguments["year"], arguments["month"])

        return calculate_savings_rate(
            customer_id,
            arguments["year"],
            arguments["month"]
        )

    # Unusual spending
    elif tool_name == "detect_unusual_spending":
        validate_arguments(
            arguments,
            ["year", "month"],
            ["year", "month"]
        )

        validate_month(arguments["year"], arguments["month"])

        return detect_unusual_spending(
            customer_id,
            arguments["year"],
            arguments["month"]
        )

    # Budget recommendations
    elif tool_name == "generate_budget_recommendations":
        validate_arguments(
            arguments,
            ["year", "month"],
            ["year", "month"]
        )

        validate_month(arguments["year"], arguments["month"])

        return generate_budget_recommendations(
            customer_id,
            arguments["year"],
            arguments["month"]
        )

    # Complete advisory report
    elif tool_name == "generate_financial_advisory_report":
        validate_arguments(
            arguments,
            ["year", "month"],
            ["year", "month"]
        )

        validate_month(arguments["year"], arguments["month"])

        return generate_financial_advisory_report(
            customer_id,
            arguments["year"],
            arguments["month"]
        )

    # Complete customer context
    elif tool_name == "get_customer_context":
        validate_arguments(arguments, ["year", "month"])

        year = arguments.get("year")
        month = arguments.get("month")

        if (year is None) != (month is None):
            raise ValueError(
                "Provide both year and month, or neither."
            )

        if year is not None:
            validate_month(year, month)

        return get_customer_context(
            customer_id,
            year,
            month
        )

    else:
        raise ValueError(
            f"Tool '{tool_name}' is not authorized."
        )


def get_available_tools():
    """Return the approved tool definitions."""

    return TOOL_DEFINITIONS.copy()
