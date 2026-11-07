"""Authenticated self-service reads for the synthetic demo dashboard."""

from datetime import date
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query

from backend.api_models import (
    SelfServiceAccount,
    SelfServiceBalance,
    SelfServiceCard,
    SelfServiceFinancialSummary,
    SelfServiceLoan,
    SelfServiceTransaction,
)
from backend.auth import get_authenticated_principal
from services.banking_service import (
    get_account_balance,
    get_card_details,
    get_customer_profile_by_key,
    get_loan_details,
    get_recent_transactions,
    search_transactions,
    toggle_card_freeze,
)
from services.customer_context import get_customer_context, get_latest_transaction_period
from services.financial_advisor import (
    compare_monthly_spending,
    generate_financial_advisory_report,
)
from services.financial_intelligence import (
    get_category_breakdown,
    get_financial_insights,
    get_top_merchants,
)
from services.principal import CustomerPrincipal
from services.transaction_risk_persistence import ensure_transaction_public_refs


router = APIRouter(prefix="/api/v1/me", tags=["Authenticated Customer Data"])


def _customer_id(principal: CustomerPrincipal) -> int:
    """Resolve the internal ID only from the authenticated stable customer key."""
    profile = get_customer_profile_by_key(principal.customer_key)
    if profile is None:
        raise HTTPException(status_code=404, detail="Customer not found.")
    return profile["customer_id"]


def _mask_account_number(value: str | None) -> str:
    value = str(value or "")
    return f"••••{value[-4:]}" if value else ""


def _project_accounts(accounts: list[dict]) -> list[dict]:
    return [
        {
            "account_type": account.get("account_type", ""),
            "account_number": _mask_account_number(account.get("account_number")),
            "balance": account.get("balance", 0),
            "status": account.get("status", ""),
        }
        for account in accounts
    ]


def _safe_accounts(customer_id: int) -> list[dict]:
    return _project_accounts(get_account_balance(customer_id))


def _safe_transactions(
    rows: list[dict], customer_key: str | None = None
) -> list[dict]:
    refs: dict[int, str] = {}
    if customer_key is not None:
        refs = ensure_transaction_public_refs(
            customer_key,
            [row.get("transaction_id") for row in rows],
        )
    projected = []
    for row in rows:
        transaction_id = row.get("transaction_id")
        item = {key: value for key, value in row.items() if key != "transaction_id"}
        if customer_key is not None:
            item["transaction_ref"] = refs.get(transaction_id)
        projected.append(item)
    return projected


def _safe_advisory(report: dict) -> dict:
    report = dict(report)
    report.pop("customer_id", None)
    return report


def _period(customer_id: int, year: int | None, month: int | None) -> tuple[int, int]:
    if (year is None) != (month is None):
        raise HTTPException(status_code=422, detail="year and month must be provided together.")
    if year is None:
        return get_latest_transaction_period(customer_id)
    return year, month


@router.get("/context")
def my_context(
    year: int | None = Query(default=None, ge=2000, le=2100),
    month: int | None = Query(default=None, ge=1, le=12),
    principal: CustomerPrincipal = Depends(get_authenticated_principal),
):
    """Safe dashboard aggregate preserving the existing Overview data shape."""
    customer_id = _customer_id(principal)
    year, month = _period(customer_id, year, month)
    try:
        context = get_customer_context(principal.customer_key, year, month)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None

    customer = context.get("customer", {})
    context["customer"] = {
        key: customer[key]
        for key in ("name", "monthly_income")
        if key in customer
    }
    banking = context.get("banking", {})
    banking["account_balance"] = _project_accounts(
        banking.get("account_balance", [])
    )
    banking["recent_transactions"] = _safe_transactions(
        banking.get("recent_transactions", [])
    )
    banking["cards"] = [
        {key: value for key, value in card.items() if key != "card_id"}
        for card in banking.get("cards", [])
    ]
    banking["loans"] = [
        {key: value for key, value in loan.items() if key != "loan_id"}
        for loan in banking.get("loans", [])
    ]
    metadata = context.get("metadata", {})
    metadata.pop("customer_id", None)
    metadata.pop("customer_key", None)
    context.get("financial_advisory", {}).pop("customer_id", None)
    return context


@router.get("/accounts", response_model=list[SelfServiceAccount])
def my_accounts(principal: CustomerPrincipal = Depends(get_authenticated_principal)):
    return _safe_accounts(_customer_id(principal))


@router.get("/balance", response_model=SelfServiceBalance)
def my_balance(principal: CustomerPrincipal = Depends(get_authenticated_principal)):
    accounts = _safe_accounts(_customer_id(principal))
    return {
        "total_balance": round(sum(float(account["balance"]) for account in accounts), 2),
        "currency": "INR",
    }


@router.get("/transactions", response_model=list[SelfServiceTransaction])
def my_transactions(
    limit: int = Query(default=100, ge=1, le=100),
    principal: CustomerPrincipal = Depends(get_authenticated_principal),
):
    return _safe_transactions(
        get_recent_transactions(_customer_id(principal), limit), principal.customer_key
    )


@router.get("/transactions/search", response_model=list[SelfServiceTransaction])
def my_transaction_search(
    merchant: str | None = Query(default=None, max_length=160),
    category: str | None = Query(default=None, max_length=80),
    transaction_type: Literal["DEBIT", "CREDIT"] | None = None,
    min_amount: float | None = Query(default=None, ge=0),
    max_amount: float | None = Query(default=None, ge=0),
    start_date: date | None = None,
    end_date: date | None = None,
    principal: CustomerPrincipal = Depends(get_authenticated_principal),
):
    if (start_date is None) != (end_date is None):
        raise HTTPException(status_code=422, detail="start_date and end_date must be provided together.")
    if min_amount is not None and max_amount is not None and min_amount > max_amount:
        raise HTTPException(status_code=422, detail="min_amount cannot exceed max_amount.")
    rows = search_transactions(
        _customer_id(principal),
        merchant=merchant,
        category=category,
        transaction_type=transaction_type,
        min_amount=min_amount,
        max_amount=max_amount,
        start_date=start_date.isoformat() if start_date else None,
        end_date=end_date.isoformat() if end_date else None,
    )
    return _safe_transactions(rows, principal.customer_key)


@router.get("/cards", response_model=list[SelfServiceCard])
def my_cards(principal: CustomerPrincipal = Depends(get_authenticated_principal)):
    rows = get_card_details(_customer_id(principal))
    return [{key: value for key, value in row.items() if key != "card_id"} for row in rows]


@router.post("/cards/{last_four}/freeze")
def freeze_card(
    last_four: str,
    freeze: bool | None = Query(default=None),
    principal: CustomerPrincipal = Depends(get_authenticated_principal),
):
    result = toggle_card_freeze(_customer_id(principal), last_four, freeze=freeze)
    if not result:
        raise HTTPException(status_code=404, detail="Card not found.")
    return result


@router.get("/loans", response_model=list[SelfServiceLoan])
def my_loans(principal: CustomerPrincipal = Depends(get_authenticated_principal)):
    rows = get_loan_details(_customer_id(principal))
    return [{key: value for key, value in row.items() if key != "loan_id"} for row in rows]


@router.get("/financial/summary", response_model=SelfServiceFinancialSummary)
def my_financial_summary(principal: CustomerPrincipal = Depends(get_authenticated_principal)):
    result = get_financial_insights(_customer_id(principal))
    return {**result["summary"], "insights": result["insights"]}


@router.get("/financial/categories")
def my_financial_categories(principal: CustomerPrincipal = Depends(get_authenticated_principal)):
    return get_category_breakdown(_customer_id(principal))


@router.get("/financial/merchants")
def my_financial_merchants(
    limit: int = Query(default=5, ge=1, le=50),
    principal: CustomerPrincipal = Depends(get_authenticated_principal),
):
    return get_top_merchants(_customer_id(principal), limit)


@router.get("/financial/monthly-comparison")
def my_monthly_comparison(
    year: int | None = Query(default=None, ge=2000, le=2100),
    month: int | None = Query(default=None, ge=1, le=12),
    principal: CustomerPrincipal = Depends(get_authenticated_principal),
):
    customer_id = _customer_id(principal)
    year, month = _period(customer_id, year, month)
    return compare_monthly_spending(customer_id, year, month)


@router.get("/financial/advisory")
def my_financial_advisory(
    year: int | None = Query(default=None, ge=2000, le=2100),
    month: int | None = Query(default=None, ge=1, le=12),
    principal: CustomerPrincipal = Depends(get_authenticated_principal),
):
    customer_id = _customer_id(principal)
    year, month = _period(customer_id, year, month)
    try:
        return _safe_advisory(generate_financial_advisory_report(customer_id, year, month))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None


# --------------------------------------------------
# NOTIFICATION CENTER
# --------------------------------------------------

@router.get("/notifications")
def my_notifications(
    limit: int = Query(default=25, ge=1, le=100),
    principal: CustomerPrincipal = Depends(get_authenticated_principal),
):
    from services.notification_service import get_unread_count, list_notifications
    items = list_notifications(principal.customer_key, limit=limit)
    unread = get_unread_count(principal.customer_key)
    return {"notifications": items, "unread_count": unread}


@router.post("/notifications/{notification_id}/read")
def my_mark_notification_read(
    notification_id: str,
    principal: CustomerPrincipal = Depends(get_authenticated_principal),
):
    from services.notification_service import mark_notification_read
    success = mark_notification_read(principal.customer_key, notification_id)
    if not success:
        raise HTTPException(status_code=404, detail="Notification not found.")
    return {"status": "success", "notification_id": notification_id}


@router.post("/notifications/read-all")
def my_mark_all_notifications_read(
    principal: CustomerPrincipal = Depends(get_authenticated_principal),
):
    from services.notification_service import get_unread_count, mark_all_notifications_read
    updated = mark_all_notifications_read(principal.customer_key)
    unread = get_unread_count(principal.customer_key)
    return {"status": "success", "marked_read": updated, "unread_count": unread}

