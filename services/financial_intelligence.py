
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
            "total_spent": round(row["total_amount"], 2),
            "amount": round(row["total_amount"], 2),
            "transaction_count": row["transaction_count"],
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


def get_monthly_transaction_summary(customer_id, year, month):
    """Return scoped monthly credit/debit totals and record counts.

    Only successful credits/debits contribute to money totals. Counts include
    all matching records and separately report successful/non-successful rows.
    """
    if not 2000 <= int(year) <= 2100 or not 1 <= int(month) <= 12:
        raise ValueError("A valid calendar month is required.")

    conn = get_connection()
    try:
        row = conn.execute("""
            SELECT
                COUNT(*) AS transaction_count,
                SUM(CASE WHEN UPPER(t.status) = 'SUCCESS' THEN 1 ELSE 0 END) AS successful_count,
                SUM(CASE WHEN UPPER(t.status) <> 'SUCCESS' THEN 1 ELSE 0 END) AS non_successful_count,
                SUM(CASE WHEN UPPER(t.status) = 'SUCCESS' AND UPPER(t.transaction_type) = 'CREDIT' THEN t.amount ELSE 0 END) AS successful_credits,
                SUM(CASE WHEN UPPER(t.status) = 'SUCCESS' AND UPPER(t.transaction_type) = 'DEBIT' THEN t.amount ELSE 0 END) AS successful_debits
            FROM transactions t
            JOIN accounts a ON a.account_id = t.account_id
            WHERE a.customer_id = ?
              AND strftime('%Y', t.transaction_date) = ?
              AND strftime('%m', t.transaction_date) = ?
        """, (customer_id, str(int(year)), f"{int(month):02d}")).fetchone()
    finally:
        conn.close()

    return {
        "year": int(year),
        "month": int(month),
        "scope": "calendar_month",
        "transaction_count": int(row["transaction_count"] or 0),
        "successful_count": int(row["successful_count"] or 0),
        "non_successful_count": int(row["non_successful_count"] or 0),
        "successful_credits": round(float(row["successful_credits"] or 0), 2),
        "successful_debits": round(float(row["successful_debits"] or 0), 2),
    }


def find_large_transactions(customer_id, threshold=10000, limit=None):
    """Find successful transactions above a specified amount."""

    if threshold < 0:
        raise ValueError("Threshold cannot be negative.")
    if limit is not None and (isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100):
        raise ValueError("Limit must be between 1 and 100.")

    conn = get_connection()

    query = """
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
AND t.amount > ?

        ORDER BY t.amount DESC, t.transaction_date DESC, t.transaction_id DESC
    """
    params = [customer_id, threshold]
    if limit is not None:
        query += " LIMIT ?"
        params.append(limit)
    rows = conn.execute(query, params).fetchall()

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
