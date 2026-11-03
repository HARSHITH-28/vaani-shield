
import os
import secrets

from dotenv import load_dotenv
from fastapi import APIRouter, Header, HTTPException, Query

from services.banking_service import (
    get_account_balance,
    get_customer_profile,
    get_recent_transactions,
    search_transactions,
    get_card_details,
    request_card_block,
    confirm_card_block,
)

load_dotenv()

router = APIRouter(
    prefix="/api/agent",
    tags=["Gnani Agent Actions"],
)

DEMO_CUSTOMER_ID = 4


def verify_action_token(token: str | None):
    expected_token = os.getenv("VAANISHIELD_ACTION_TOKEN")

    if not expected_token:
        raise HTTPException(
            status_code=503,
            detail="Agent authentication is not configured.",
        )

    if not token or not secrets.compare_digest(token, expected_token):
        raise HTTPException(
            status_code=401,
            detail="Invalid or missing authentication token.",
        )


def get_demo_profile():
    profile = get_customer_profile(DEMO_CUSTOMER_ID)

    if not profile:
        raise HTTPException(
            status_code=404,
            detail="Demo customer not found.",
        )

    return profile


@router.post("/account-balance")
def agent_account_balance(
    x_vaanishield_token: str | None = Header(default=None),
):
    verify_action_token(x_vaanishield_token)
    profile = get_demo_profile()

    accounts = get_account_balance(DEMO_CUSTOMER_ID)

    if not accounts:
        raise HTTPException(
            status_code=404,
            detail="Account information unavailable.",
        )

    return {
        "success": True,
        "tool": "get_account_balance",
        "customer": {
            "customer_id": DEMO_CUSTOMER_ID,
            "name": profile["name"],
        },
        "accounts": accounts,
        "currency": "INR",
        "data_type": "synthetic_demo_data",
    }


@router.post("/recent-transactions")
def agent_recent_transactions(
    x_vaanishield_token: str | None = Header(default=None),
    limit: int = Query(default=5, ge=1, le=10),
):
    verify_action_token(x_vaanishield_token)
    profile = get_demo_profile()

    transactions = get_recent_transactions(
        DEMO_CUSTOMER_ID,
        limit=limit,
    )

    formatted_transactions = []

    for index, txn in enumerate(transactions, start=1):
        amount = float(txn.get("amount") or 0)

        formatted_transactions.append(
            f"{index}. "
            f"{txn.get('transaction_date', 'Date unavailable')} | "
            f"{txn.get('merchant') or 'Merchant unavailable'} | "
            f"INR {amount:,.2f} | "
            f"{txn.get('category') or 'Category unavailable'} | "
            f"{txn.get('transaction_type') or 'Type unavailable'} | "
            f"{txn.get('status') or 'Status unavailable'}"
        )

    return {
        "success": True,
        "tool": "get_recent_transactions",
        "customer_name": profile["name"],
        "transaction_count": len(transactions),
        "transaction_data": (
            "\n".join(formatted_transactions)
            if formatted_transactions
            else "No transactions found."
        ),
        "currency": "INR",
        "data_type": "synthetic_demo_data",
    }


@router.post("/search-transactions")
def agent_search_transactions(
    x_vaanishield_token: str | None = Header(default=None),
    merchant: str | None = None,
    category: str | None = None,
    transaction_type: str | None = None,
    min_amount: float | None = Query(default=None, ge=0),
    max_amount: float | None = Query(default=None, ge=0),
    start_date: str | None = None,
    end_date: str | None = None,
):
    verify_action_token(x_vaanishield_token)

    if (
        min_amount is not None
        and max_amount is not None
        and min_amount > max_amount
    ):
        raise HTTPException(
            status_code=400,
            detail="Minimum amount cannot exceed maximum amount.",
        )

    if start_date and end_date and start_date > end_date:
        raise HTTPException(
            status_code=400,
            detail="Start date cannot be after end date.",
        )

    profile = get_demo_profile()

    transactions = search_transactions(
        customer_id=DEMO_CUSTOMER_ID,
        merchant=merchant,
        category=category,
        transaction_type=transaction_type,
        min_amount=min_amount,
        max_amount=max_amount,
        start_date=start_date,
        end_date=end_date,
    )

    formatted = []

    for index, txn in enumerate(transactions, start=1):
        formatted.append(
            f"{index}. "
            f"{txn.get('transaction_date')} | "
            f"{txn.get('merchant')} | "
            f"INR {float(txn.get('amount') or 0):,.2f} | "
            f"{txn.get('category')} | "
            f"{txn.get('transaction_type')} | "
            f"{txn.get('status')}"
        )

    return {
        "success": True,
        "tool": "search_transactions",
        "customer_name": profile["name"],
        "transaction_count": len(transactions),
        "transaction_data": (
            "\n".join(formatted)
            if formatted
            else "No matching transactions found."
        ),
        "filters_applied": {
            "merchant": merchant,
            "category": category,
            "transaction_type": transaction_type,
            "min_amount": min_amount,
            "max_amount": max_amount,
            "start_date": start_date,
            "end_date": end_date,
        },
        "currency": "INR",
        "data_type": "synthetic_demo_data",
    }


# --------------------------------------------------
# CARD MANAGEMENT
# --------------------------------------------------

@router.post("/card-details")
def agent_card_details(
    x_vaanishield_token: str | None = Header(default=None),
):
    verify_action_token(x_vaanishield_token)
    profile = get_demo_profile()

    cards = get_card_details(DEMO_CUSTOMER_ID)

    return {
        "success": True,
        "tool": "get_card_details",
        "customer_name": profile["name"],
        "card_count": len(cards),
        "cards": cards,
        "data_type": "synthetic_demo_data",
    }


@router.post("/request-card-block")
def agent_request_card_block(
    card_id: int = Query(..., ge=1),
    x_vaanishield_token: str | None = Header(default=None),
):
    verify_action_token(x_vaanishield_token)
    profile = get_demo_profile()

    result = request_card_block(
        customer_id=DEMO_CUSTOMER_ID,
        card_id=card_id,
    )

    if result is None:
        raise HTTPException(
            status_code=404,
            detail="Card not found for this demo customer.",
        )

    if result.get("already_blocked"):
        raise HTTPException(
            status_code=409,
            detail="This card is already blocked.",
        )

    if not result.get("request_pending"):
        raise HTTPException(
            status_code=409,
            detail=result.get("message", "Card cannot be blocked."),
        )

    card = result["card"]

    return {
        "success": True,
        "tool": "request_card_block",
        "customer_name": profile["name"],
        "card_id": card["card_id"],
        "card_type": card["card_type"],
        "last_four": card["last_four"],
        "current_status": card["status"],
        "requires_confirmation": True,
        "card_blocked": False,
        "message": (
            "A card-block request is pending. "
            "The card is still ACTIVE. Ask the customer for explicit "
            "confirmation before calling confirm_card_block."
        ),
        "data_type": "synthetic_demo_data",
    }


@router.post("/confirm-card-block")
def agent_confirm_card_block(
    card_id: int = Query(..., ge=1),
    confirmation_phrase: str = Query(...),
    x_vaanishield_token: str | None = Header(default=None),
):
    verify_action_token(x_vaanishield_token)

    if confirmation_phrase.strip().upper() != "CONFIRM BLOCK":
        raise HTTPException(
            status_code=400,
            detail="Explicit confirmation phrase is required.",
        )

    profile = get_demo_profile()

    result = confirm_card_block(
        customer_id=DEMO_CUSTOMER_ID,
        card_id=card_id,
    )

    if result is None:
        raise HTTPException(
            status_code=404,
            detail="Card not found for this demo customer.",
        )

    if not result.get("blocked"):
        raise HTTPException(
            status_code=409,
            detail=result.get("message", "No pending card-block request."),
        )

    return {
        "success": True,
        "tool": "confirm_card_block",
        "customer_name": profile["name"],
        "card": result["card"],
        "card_blocked": True,
        "already_blocked": result["already_blocked"],
        "message": result["message"],
        "data_type": "synthetic_demo_data",
    }
