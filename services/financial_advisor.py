
from services.database import get_connection
from services.financial_intelligence import (
    get_monthly_spending,
    get_category_breakdown,
)


def get_customer_income(customer_id):
    """Retrieve the customer's recorded monthly income."""

    conn = get_connection()

    row = conn.execute("""
        SELECT monthly_income
        FROM customers
        WHERE customer_id = ?
    """, (customer_id,)).fetchone()

    conn.close()

    if row is None:
        raise ValueError("Customer not found.")

    return float(row["monthly_income"])


def get_previous_month(year, month):
    """Calculate the previous calendar month."""

    if month == 1:
        return year - 1, 12

    return year, month - 1


def compare_monthly_spending(customer_id, year, month):
    """Compare spending with the previous month."""

    current = get_monthly_spending(
        customer_id, year, month
    )

    previous_year, previous_month = get_previous_month(
        year, month
    )

    previous = get_monthly_spending(
        customer_id, previous_year, previous_month
    )

    current_spending = current["total_spent"]
    previous_spending = previous["total_spent"]

    difference = current_spending - previous_spending

    if previous_spending > 0:
        percentage_change = (
            difference / previous_spending
        ) * 100
    else:
        percentage_change = None

    if difference > 0:
        trend = "INCREASED"
    elif difference < 0:
        trend = "DECREASED"
    else:
        trend = "UNCHANGED"

    return {
        "current_month": f"{year}-{month:02d}",
        "previous_month": (
            f"{previous_year}-{previous_month:02d}"
        ),
        "current_spending": round(current_spending, 2),
        "previous_spending": round(previous_spending, 2),
        "difference": round(difference, 2),
        "percentage_change": (
            round(percentage_change, 2)
            if percentage_change is not None
            else None
        ),
        "trend": trend,
    }


def calculate_savings_rate(customer_id, year, month):
    """Calculate monthly savings using recorded income and spending."""

    income = get_customer_income(customer_id)

    spending = get_monthly_spending(
        customer_id, year, month
    )["total_spent"]

    savings = income - spending

    savings_rate = (
        (savings / income) * 100
        if income > 0
        else 0
    )

    return {
        "monthly_income": round(income, 2),
        "monthly_spending": round(spending, 2),
        "estimated_savings": round(savings, 2),
        "savings_rate": round(savings_rate, 2),
    }


def detect_unusual_spending(customer_id, year, month):
    """
    Flag categories whose spending is significantly higher
    than the average of the previous two months.

    This is a simple explainable heuristic, not a fraud detector.
    """

    previous_year, previous_month = get_previous_month(
        year, month
    )

    second_previous_year, second_previous_month = (
        get_previous_month(previous_year, previous_month)
    )

    periods = [
        (year, month),
        (previous_year, previous_month),
        (second_previous_year, second_previous_month),
    ]

    conn = get_connection()

    rows = conn.execute("""
        SELECT
            t.category,
            CAST(strftime('%Y', t.transaction_date) AS INTEGER) AS year,
            CAST(strftime('%m', t.transaction_date) AS INTEGER) AS month,
            SUM(t.amount) AS total_spending

        FROM transactions t
        JOIN accounts a ON t.account_id = a.account_id

        WHERE a.customer_id = ?
        AND t.transaction_type = 'DEBIT'
        AND t.status = 'SUCCESS'

        GROUP BY
            t.category,
            strftime('%Y', t.transaction_date),
            strftime('%m', t.transaction_date)
    """, (customer_id,)).fetchall()

    conn.close()

    spending = {}

    for row in rows:
        key = (
            row["category"],
            row["year"],
            row["month"],
        )
        spending[key] = row["total_spending"]

    current_year, current_month = periods[0]
    previous_periods = periods[1:]

    categories = {
        row["category"] for row in rows
    }

    unusual = []

    for category in categories:
        current_amount = spending.get(
            (category, current_year, current_month), 0
        )

        historical_values = [
            spending.get((category, y, m), 0)
            for y, m in previous_periods
        ]

        historical_average = (
            sum(historical_values) / len(historical_values)
        )

        # Require both a significant relative and absolute increase.
        if (
            historical_average > 0
            and current_amount > historical_average * 1.5
            and current_amount - historical_average >= 500
        ):
            unusual.append({
                "category": category,
                "current_spending": round(current_amount, 2),
                "historical_average": round(
                    historical_average, 2
                ),
                "increase": round(
                    current_amount - historical_average, 2
                ),
                "increase_percentage": round(
                    (
                        (current_amount - historical_average)
                        / historical_average
                    ) * 100,
                    2
                ),
            })

    return sorted(
        unusual,
        key=lambda item: item["increase"],
        reverse=True
    )


def generate_budget_recommendations(customer_id, year, month):
    """
    Generate illustrative 50/30/20 budget observations.

    Needs: 50% of income
    Wants: 30% of income
    Savings target: 20% of income
    """

    income = get_customer_income(customer_id)

    breakdown = get_category_breakdown(customer_id)

    needs_categories = {
        "FOOD",
        "GROCERIES",
        "UTILITIES",
        "HEALTH",
        "TRANSPORT",
        "FUEL",
    }

    wants_categories = {
        "SHOPPING",
        "SUBSCRIPTION",
    }

    needs_spending = sum(
        item["total_amount"]
        for item in breakdown
        if item["category"] in needs_categories
    )

    wants_spending = sum(
        item["total_amount"]
        for item in breakdown
        if item["category"] in wants_categories
    )

    # Use the requested month's actual spending.
    monthly_spending = get_monthly_spending(
        customer_id, year, month
    )["total_spent"]

    # Retrieve category totals specifically for the requested month.
    conn = get_connection()

    monthly_rows = conn.execute("""
        SELECT
            t.category,
            SUM(t.amount) AS total_amount

        FROM transactions t
        JOIN accounts a ON t.account_id = a.account_id

        WHERE a.customer_id = ?
        AND t.transaction_type = 'DEBIT'
        AND t.status = 'SUCCESS'
        AND strftime('%Y', t.transaction_date) = ?
        AND strftime('%m', t.transaction_date) = ?

        GROUP BY t.category
    """, (customer_id, str(year), f"{month:02d}")).fetchall()

    conn.close()

    needs_spending = sum(
        row["total_amount"]
        for row in monthly_rows
        if row["category"] in needs_categories
    )

    wants_spending = sum(
        row["total_amount"]
        for row in monthly_rows
        if row["category"] in wants_categories
    )

    needs_budget = income * 0.50
    wants_budget = income * 0.30
    savings_target = income * 0.20

    estimated_savings = income - monthly_spending

    recommendations = []

    if needs_spending > needs_budget:
        recommendations.append(
            "Essential-category spending is above the "
            "illustrative 50% budget allocation."
        )

    if wants_spending > wants_budget:
        recommendations.append(
            "Lifestyle-category spending is above the "
            "illustrative 30% budget allocation."
        )

    if estimated_savings < savings_target:
        recommendations.append(
            "Recorded savings are below the illustrative "
            "20% savings target."
        )

    if not recommendations:
        recommendations.append(
            "Recorded spending is within the illustrative "
            "50/30/20 budget allocations."
        )

    return {
        "monthly_income": round(income, 2),
        "monthly_spending": round(monthly_spending, 2),
        "needs_spending": round(needs_spending, 2),
        "needs_budget": round(needs_budget, 2),
        "wants_spending": round(wants_spending, 2),
        "wants_budget": round(wants_budget, 2),
        "estimated_savings": round(estimated_savings, 2),
        "savings_target": round(savings_target, 2),
        "recommendations": recommendations,
        "methodology": "Illustrative 50/30/20 budgeting heuristic",
    }


def generate_financial_advisory_report(customer_id, year, month):
    """Combine financial analysis into one advisory report."""

    return {
        "customer_id": customer_id,
        "period": f"{year}-{month:02d}",
        "spending_comparison": compare_monthly_spending(
            customer_id, year, month
        ),
        "savings_analysis": calculate_savings_rate(
            customer_id, year, month
        ),
        "unusual_spending": detect_unusual_spending(
            customer_id, year, month
        ),
        "budget_analysis": generate_budget_recommendations(
            customer_id, year, month
        ),
    }
