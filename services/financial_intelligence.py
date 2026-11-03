
from services.database import get_connection


def get_spending_summary(customer_id):
    """Calculate total credits, debits and net cash flow."""

    conn = get_connection()

    result = conn.execute("""
        SELECT
            SUM(CASE
                WHEN t.transaction_type = 'DEBIT'
                AND t.status = 'SUCCESS'
                THEN t.amount ELSE 0 END) AS total_spent,

            SUM(CASE
                WHEN t.transaction_type = 'CREDIT'
                AND t.status = 'SUCCESS'
                THEN t.amount ELSE 0 END) AS total_credited

        FROM transactions t
        JOIN accounts a ON t.account_id = a.account_id
        WHERE a.customer_id = ?
    """, (customer_id,)).fetchone()

    conn.close()

    spent = result["total_spent"] or 0
    credited = result["total_credited"] or 0

    return {
        "total_spent": round(spent, 2),
        "total_credited": round(credited, 2),
        "net_cash_flow": round(credited - spent, 2)
    }


def get_category_breakdown(customer_id):
    """Calculate successful spending grouped by category."""

    conn = get_connection()

    rows = conn.execute("""
        SELECT
            t.category,
            SUM(t.amount) AS total_amount,
            COUNT(*) AS transaction_count

        FROM transactions t
        JOIN accounts a ON t.account_id = a.account_id

        WHERE a.customer_id = ?
        AND t.transaction_type = 'DEBIT'
        AND t.status = 'SUCCESS'

        GROUP BY t.category
        ORDER BY total_amount DESC
    """, (customer_id,)).fetchall()

    conn.close()

    return [
        {
            "category": row["category"],
            "total_amount": round(row["total_amount"], 2),
            "transaction_count": row["transaction_count"]
        }
        for row in rows
    ]


def get_top_merchants(customer_id, limit=5):
    """Identify merchants with the highest total spending."""

    limit = max(1, min(int(limit), 50))

    conn = get_connection()

    rows = conn.execute("""
        SELECT
            t.merchant,
            SUM(t.amount) AS total_spent,
            COUNT(*) AS transaction_count

        FROM transactions t
        JOIN accounts a ON t.account_id = a.account_id

        WHERE a.customer_id = ?
        AND t.transaction_type = 'DEBIT'
        AND t.status = 'SUCCESS'

        GROUP BY t.merchant
        ORDER BY total_spent DESC
        LIMIT ?
    """, (customer_id, limit)).fetchall()

    conn.close()

    return [dict(row) for row in rows]


def get_monthly_spending(customer_id, year, month):
    """Calculate successful debit spending for a given month."""

    if not 1 <= month <= 12:
        raise ValueError("Month must be between 1 and 12.")

    conn = get_connection()

    result = conn.execute("""
        SELECT
            SUM(t.amount) AS total_spent,
            COUNT(*) AS transaction_count

        FROM transactions t
        JOIN accounts a ON t.account_id = a.account_id

        WHERE a.customer_id = ?
        AND t.transaction_type = 'DEBIT'
        AND t.status = 'SUCCESS'
        AND strftime('%Y', t.transaction_date) = ?
        AND strftime('%m', t.transaction_date) = ?
    """, (customer_id, str(year), f"{month:02d}")).fetchone()

    conn.close()

    return {
        "year": year,
        "month": month,
        "total_spent": round(result["total_spent"] or 0, 2),
        "transaction_count": result["transaction_count"]
    }


def find_large_transactions(customer_id, threshold=10000):
    """Find successful transactions above a specified amount."""

    if threshold < 0:
        raise ValueError("Threshold cannot be negative.")

    conn = get_connection()

    rows = conn.execute("""
        SELECT
            t.transaction_id,
            t.amount,
            t.transaction_type,
            t.merchant,
            t.category,
            t.transaction_date,
            t.reference_id

        FROM transactions t
        JOIN accounts a ON t.account_id = a.account_id

        WHERE a.customer_id = ?
        AND t.status = 'SUCCESS'
        AND t.transaction_type = 'DEBIT'
AND t.amount >= ?

        ORDER BY t.amount DESC
    """, (customer_id, threshold)).fetchall()

    conn.close()

    return [dict(row) for row in rows]


def get_financial_insights(customer_id):
    """Generate simple explainable spending observations."""

    breakdown = get_category_breakdown(customer_id)
    summary = get_spending_summary(customer_id)

    insights = []

    if breakdown:
        highest = breakdown[0]

        insights.append({
            "type": "TOP_SPENDING_CATEGORY",
            "message": (
                f"{highest['category']} is your highest "
                f"spending category at ₹{highest['total_amount']:.2f}."
            )
        })

    if summary["net_cash_flow"] < 0:
        insights.append({
            "type": "NEGATIVE_CASH_FLOW",
            "message": "Your recorded successful credits are lower than your debits."
        })

    if not breakdown:
        insights.append({
            "type": "NO_SPENDING_DATA",
            "message": "There is not enough successful debit data to analyse spending."
        })

    return {
        "summary": summary,
        "insights": insights
    }
