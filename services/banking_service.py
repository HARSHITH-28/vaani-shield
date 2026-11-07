
from services.database import get_connection


# --------------------------------------------------
# CUSTOMER PROFILE
# --------------------------------------------------

def get_customer_profile(customer_id):
    conn = get_connection()
    try:
        customer = conn.execute("""
            SELECT customer_id, customer_key, name, email, phone, monthly_income
            FROM customers
            WHERE customer_id = ?
        """, (customer_id,)).fetchone()

        return dict(customer) if customer else None
    finally:
        conn.close()


def get_customer_profile_by_key(customer_key):
    """Retrieve a customer using their stable public key."""
    conn = get_connection()
    try:
        customer = conn.execute("""
            SELECT customer_id, customer_key, name, email, phone, monthly_income
            FROM customers
            WHERE customer_key = ?
        """, (customer_key,)).fetchone()

        return dict(customer) if customer else None
    finally:
        conn.close()


def get_customer_profile_by_reference(customer_reference):
    """Resolve a stable customer key, with numeric-ID compatibility."""
    if isinstance(customer_reference, int):
        return get_customer_profile(customer_reference)

    reference = str(customer_reference).strip()
    if reference.isdecimal():
        return get_customer_profile(int(reference))

    return get_customer_profile_by_key(reference)


def list_customers():
    """Return the customer keys and display names used by the demo UI."""
    conn = get_connection()
    try:
        customers = conn.execute("""
            SELECT customer_key, name
            FROM customers
            ORDER BY name, customer_key
        """).fetchall()

        return [dict(row) for row in customers]
    finally:
        conn.close()


# --------------------------------------------------
# ACCOUNT BALANCE
# --------------------------------------------------

def get_account_balance(customer_id):
    profile = get_customer_profile_by_reference(customer_id)
    if profile:
        customer_id = profile["customer_id"]
    conn = get_connection()
    try:
        accounts = conn.execute("""
            SELECT account_number, account_type, balance, status
            FROM accounts
            WHERE customer_id = ?
        """, (customer_id,)).fetchall()

        return [dict(row) for row in accounts]
    finally:
        conn.close()


# --------------------------------------------------
# RECENT TRANSACTIONS
# --------------------------------------------------

def get_recent_transactions(customer_id, limit=10):
    profile = get_customer_profile_by_reference(customer_id)
    if profile:
        customer_id = profile["customer_id"]
    limit = max(1, min(int(limit), 100))

    conn = get_connection()
    try:
        transactions = conn.execute("""
            SELECT
                t.transaction_id,
                t.transaction_type,
                t.amount,
                t.merchant,
                t.category,
                t.status,
                t.reference_id,
                t.transaction_date,
                t.description
            FROM transactions t
            JOIN accounts a ON t.account_id = a.account_id
            WHERE a.customer_id = ?
            ORDER BY t.transaction_date DESC,
                     t.transaction_id DESC
            LIMIT ?
        """, (customer_id, limit)).fetchall()

        return [dict(row) for row in transactions]
    finally:
        conn.close()


# --------------------------------------------------
# TRANSACTION SEARCH
# --------------------------------------------------

def search_transactions(
    customer_id,
    merchant=None,
    category=None,
    transaction_type=None,
    min_amount=None,
    max_amount=None,
    start_date=None,
    end_date=None,
):
    query = """
        SELECT
            t.transaction_id,
            t.transaction_type,
            t.amount,
            t.merchant,
            t.category,
            t.status,
            t.reference_id,
            t.transaction_date,
            t.description
        FROM transactions t
        JOIN accounts a ON t.account_id = a.account_id
        WHERE a.customer_id = ?
    """

    params = [customer_id]

    if merchant:
        query += " AND LOWER(t.merchant) LIKE LOWER(?)"
        params.append(f"%{merchant.strip()}%")

    if category:
        query += " AND LOWER(t.category) = LOWER(?)"
        params.append(category.strip())

    if transaction_type:
        query += " AND UPPER(t.transaction_type) = UPPER(?)"
        params.append(transaction_type.strip())

    if min_amount is not None:
        query += " AND t.amount >= ?"
        params.append(min_amount)

    if max_amount is not None:
        query += " AND t.amount <= ?"
        params.append(max_amount)

    if start_date:
        query += " AND DATE(t.transaction_date) >= DATE(?)"
        params.append(start_date)

    if end_date:
        query += " AND DATE(t.transaction_date) <= DATE(?)"
        params.append(end_date)

    query += """
        ORDER BY t.transaction_date DESC,
                 t.transaction_id DESC
    """

    conn = get_connection()
    try:
        transactions = conn.execute(query, params).fetchall()
        return [dict(row) for row in transactions]
    finally:
        conn.close()


def get_transaction_by_public_reference(customer_id, transaction_ref):
    """Return one transaction only when its opaque public reference is owned by customer_id."""
    if not isinstance(transaction_ref, str) or not transaction_ref.startswith("TR-"):
        return None
    conn = get_connection()
    try:
        row = conn.execute(
            """SELECT t.transaction_type, t.amount, t.merchant, t.category,
                      t.status, t.transaction_date, t.description
               FROM transaction_public_refs p
               JOIN transactions t ON t.transaction_id = p.transaction_id
               JOIN accounts a ON a.account_id = t.account_id
               WHERE p.transaction_ref = ? AND a.customer_id = ?""",
            (transaction_ref, customer_id),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


# --------------------------------------------------
# CARD DETAILS
# --------------------------------------------------

def get_card_details(customer_id):
    profile = get_customer_profile_by_reference(customer_id)
    if profile:
        customer_id = profile["customer_id"]
    conn = get_connection()
    try:
        cards = conn.execute("""
            SELECT
                c.card_id,
                c.card_type,
                c.last_four,
                c.status,
                c.expiry_date,
                c.card_network,
                c.cardholder_name,
                c.daily_limit,
                c.monthly_limit,
                c.credit_limit,
                c.available_credit_limit,
                c.outstanding_amount,
                a.balance AS account_balance
            FROM cards c
            JOIN accounts a ON c.account_id = a.account_id
            WHERE a.customer_id = ?
            ORDER BY c.card_id
        """, (customer_id,)).fetchall()

        return [dict(row) for row in cards]
    finally:
        conn.close()


def toggle_card_freeze(customer_id: int, last_four: str, freeze: bool | None = None) -> dict | None:
    """Toggle or set freeze status on a card."""
    conn = get_connection()
    try:
        card = conn.execute("""
            SELECT c.card_id, c.card_type, c.last_four, c.status
            FROM cards c
            JOIN accounts a ON c.account_id = a.account_id
            WHERE c.last_four = ? AND a.customer_id = ?
        """, (last_four, customer_id)).fetchone()

        if not card:
            return None

        current_status = card["status"]
        if current_status == "BLOCKED":
            return {
                "card_id": card["card_id"],
                "last_four": last_four,
                "status": "BLOCKED",
                "message": "Card is permanently blocked and cannot be unfreezed.",
                "success": False,
            }

        if freeze is None:
            new_status = "ACTIVE" if current_status == "FROZEN" else "FROZEN"
        else:
            new_status = "FROZEN" if freeze else "ACTIVE"

        conn.execute("""
            UPDATE cards
            SET status = ?
            WHERE card_id = ?
        """, (new_status, card["card_id"]))
        conn.commit()

        return {
            "card_id": card["card_id"],
            "last_four": last_four,
            "status": new_status,
            "message": f"Card •••• {last_four} is now {new_status.lower()}.",
            "success": True,
        }
    finally:
        conn.close()


# --------------------------------------------------
# REQUEST CARD BLOCK
# --------------------------------------------------

def request_card_block(customer_id, card_id):
    """
    Creates a pending card-block request.
    The card remains ACTIVE until confirmation.
    """

    conn = get_connection()

    try:
        conn.execute("BEGIN IMMEDIATE")

        card = conn.execute("""
            SELECT
                c.card_id,
                c.card_type,
                c.last_four,
                c.status
            FROM cards c
            JOIN accounts a ON c.account_id = a.account_id
            WHERE c.card_id = ?
              AND a.customer_id = ?
        """, (card_id, customer_id)).fetchone()

        if not card:
            conn.rollback()
            return None

        card = dict(card)

        if card["status"].upper() != "ACTIVE":
            conn.rollback()

            return {
                "card": card,
                "request_pending": False,
                "already_blocked": card["status"].upper() == "BLOCKED",
                "message": f"Card status is {card['status']}.",
            }

        pending_details = f"card_id={card_id}|status=PENDING"

        existing = conn.execute("""
            SELECT log_id
            FROM audit_logs
            WHERE customer_id = ?
              AND action = 'CARD_BLOCK_REQUESTED'
              AND details = ?
            ORDER BY log_id DESC
            LIMIT 1
        """, (customer_id, pending_details)).fetchone()

        if existing:
            conn.commit()

            return {
                "card": card,
                "request_pending": True,
                "already_blocked": False,
                "message": "A card-block request is already awaiting confirmation.",
            }

        conn.execute("""
            INSERT INTO audit_logs (
                customer_id,
                action,
                details
            )
            VALUES (?, ?, ?)
        """, (
            customer_id,
            "CARD_BLOCK_REQUESTED",
            pending_details,
        ))

        conn.commit()

        return {
            "card": card,
            "request_pending": True,
            "already_blocked": False,
            "message": "Block request created. Card is still ACTIVE.",
        }

    except Exception:
        conn.rollback()
        raise

    finally:
        conn.close()


# --------------------------------------------------
# CONFIRM CARD BLOCK
# --------------------------------------------------

def confirm_card_block(customer_id, card_id):
    """
    Blocks a card only when a matching pending request exists.
    Updates the audit record so the same request cannot be reused.
    """

    conn = get_connection()

    try:
        conn.execute("BEGIN IMMEDIATE")

        card = conn.execute("""
            SELECT
                c.card_id,
                c.card_type,
                c.last_four,
                c.status
            FROM cards c
            JOIN accounts a ON c.account_id = a.account_id
            WHERE c.card_id = ?
              AND a.customer_id = ?
        """, (card_id, customer_id)).fetchone()

        if not card:
            conn.rollback()
            return None

        card = dict(card)

        if card["status"].upper() == "BLOCKED":
            conn.commit()

            return {
                "card": card,
                "blocked": True,
                "already_blocked": True,
                "message": "This card is already blocked.",
            }

        if card["status"].upper() != "ACTIVE":
            conn.rollback()

            return {
                "card": card,
                "blocked": False,
                "already_blocked": False,
                "message": f"Card cannot be blocked from status {card['status']}.",
            }

        pending_details = f"card_id={card_id}|status=PENDING"

        pending = conn.execute("""
            SELECT log_id
            FROM audit_logs
            WHERE customer_id = ?
              AND action = 'CARD_BLOCK_REQUESTED'
              AND details = ?
            ORDER BY log_id DESC
            LIMIT 1
        """, (customer_id, pending_details)).fetchone()

        if not pending:
            conn.rollback()

            return {
                "card": card,
                "blocked": False,
                "already_blocked": False,
                "message": "No pending card-block request exists.",
            }

        # Update card status.
        conn.execute("""
            UPDATE cards
            SET status = 'BLOCKED'
            WHERE card_id = ?
        """, (card_id,))

        # Mark the pending request as confirmed.
        # This prevents the same request from being reused.
        conn.execute("""
            UPDATE audit_logs
            SET details = ?
            WHERE log_id = ?
        """, (
            f"card_id={card_id}|status=CONFIRMED",
            pending["log_id"],
        ))

        # Record the completed card-block operation.
        conn.execute("""
            INSERT INTO audit_logs (
                customer_id,
                action,
                details
            )
            VALUES (?, ?, ?)
        """, (
            customer_id,
            "CARD_BLOCK_CONFIRMED",
            f"card_id={card_id}|status=BLOCKED",
        ))

        conn.commit()

        card["status"] = "BLOCKED"

        return {
            "card": card,
            "blocked": True,
            "already_blocked": False,
            "message": "Card successfully marked as BLOCKED in the demo database.",
        }

    except Exception:
        conn.rollback()
        raise

    finally:
        conn.close()


# --------------------------------------------------
# LOAN DETAILS
# --------------------------------------------------

def get_loan_details(customer_id):
    profile = get_customer_profile_by_reference(customer_id)
    if profile:
        customer_id = profile["customer_id"]

    conn = get_connection()

    try:
        cursor = conn.cursor()
        cursor.execute("PRAGMA table_info(loans)")
        cols = {r[1] for r in cursor.fetchall()}
        has_extended = "monthly_emi" in cols

        if has_extended:
            loans = conn.execute("""
                SELECT
                    l.loan_id,
                    l.loan_type,
                    l.principal_amount,
                    l.interest_rate,
                    l.tenure_months,
                    l.outstanding_amount,
                    l.status,
                    l.monthly_emi,
                    l.remaining_tenure_months,
                    l.next_emi_date,
                    l.completion_date
                FROM loans l
                JOIN accounts a ON l.account_id = a.account_id
                WHERE a.customer_id = ?
            """, (customer_id,)).fetchall()
        else:
            loans = conn.execute("""
                SELECT
                    l.loan_id,
                    l.loan_type,
                    l.principal_amount,
                    l.interest_rate,
                    l.tenure_months,
                    l.outstanding_amount,
                    l.status
                FROM loans l
                JOIN accounts a ON l.account_id = a.account_id
                WHERE a.customer_id = ?
            """, (customer_id,)).fetchall()

        results = []
        for row in loans:
            item = dict(row)
            if item.get("monthly_emi") is None and item.get("principal_amount") and item.get("tenure_months"):
                try:
                    item["monthly_emi"] = calculate_emi(
                        float(item["principal_amount"]),
                        float(item.get("interest_rate", 0)),
                        int(item["tenure_months"]),
                    )
                except Exception:
                    item["monthly_emi"] = None
            results.append(item)
        return results
    finally:
        conn.close()


# --------------------------------------------------
# EMI CALCULATOR
# --------------------------------------------------

def calculate_emi(principal, annual_interest_rate, tenure_months):

    if principal <= 0 or tenure_months <= 0:
        raise ValueError("Principal and tenure must be positive.")

    if annual_interest_rate < 0:
        raise ValueError("Interest rate cannot be negative.")

    monthly_rate = annual_interest_rate / (12 * 100)

    if monthly_rate == 0:
        return round(principal / tenure_months, 2)

    emi = (
        principal * monthly_rate *
        (1 + monthly_rate) ** tenure_months
    ) / (
        (1 + monthly_rate) ** tenure_months - 1
    )

    return round(emi, 2)
