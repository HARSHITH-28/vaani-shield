
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

from backend.voice_routes import router as voice_router
from backend.agent_routes import router as agent_router

from services.banking_service import (
    get_customer_profile,
    get_account_balance,
    get_recent_transactions,
    search_transactions,
    get_card_details,
    get_loan_details,
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


app = FastAPI(
    title="VaaniShield API",
    description="Synthetic banking and financial intelligence API",
    version="1.0.0",
)

app.include_router(voice_router)
app.include_router(agent_router)


# Local frontend development only.
# Restrict origins before production deployment.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:8501",
        "http://127.0.0.1:8501",
        "http://localhost:3000",
        "http://127.0.0.1:3000",
    ],
    allow_credentials=True,
    allow_methods=["GET"],
    allow_headers=["*"],
)


def validate_customer(customer_id: int):
    profile = get_customer_profile(customer_id)

    if profile is None:
        raise HTTPException(
            status_code=404,
            detail="Customer not found",
        )

    return profile


@app.get("/")
def root():
    return {
        "application": "VaaniShield",
        "status": "running",
        "data": "synthetic demonstration data",
    }


@app.get("/health")
def health():
    return {"status": "healthy"}


@app.get("/api/customers/{customer_id}/profile")
def customer_profile(customer_id: int):
    return validate_customer(customer_id)


@app.get("/api/customers/{customer_id}/balance")
def account_balance(customer_id: int):
    validate_customer(customer_id)
    return get_account_balance(customer_id)


@app.get("/api/customers/{customer_id}/transactions")
def recent_transactions(
    customer_id: int,
    limit: int = Query(default=10, ge=1, le=100),
):
    validate_customer(customer_id)
    return get_recent_transactions(customer_id, limit)


# Advanced transaction search
@app.get("/api/customers/{customer_id}/transactions/search")
def transaction_search(
    customer_id: int,
    merchant: str | None = None,
    category: str | None = None,
    transaction_type: str | None = None,
    min_amount: float | None = Query(default=None, ge=0),
    max_amount: float | None = Query(default=None, ge=0),
    start_date: str | None = None,
    end_date: str | None = None,
):
    validate_customer(customer_id)

    if min_amount is not None and max_amount is not None:
        if min_amount > max_amount:
            raise HTTPException(
                status_code=400,
                detail="min_amount cannot exceed max_amount"
            )

    return search_transactions(
        customer_id,
        merchant=merchant,
        category=category,
        transaction_type=transaction_type,
        min_amount=min_amount,
        max_amount=max_amount,
        start_date=start_date,
        end_date=end_date,
    )


@app.get("/api/customers/{customer_id}/cards")
def cards(customer_id: int):
    validate_customer(customer_id)
    return get_card_details(customer_id)


@app.get("/api/customers/{customer_id}/loans")
def loans(customer_id: int):
    validate_customer(customer_id)
    return get_loan_details(customer_id)


@app.get("/api/customers/{customer_id}/financial/summary")
def financial_summary(customer_id: int):
    validate_customer(customer_id)
    return get_spending_summary(customer_id)


@app.get("/api/customers/{customer_id}/financial/categories")
def financial_categories(customer_id: int):
    validate_customer(customer_id)
    return get_category_breakdown(customer_id)


@app.get("/api/customers/{customer_id}/financial/merchants")
def financial_merchants(
    customer_id: int,
    limit: int = Query(default=5, ge=1, le=50),
):
    validate_customer(customer_id)
    return get_top_merchants(customer_id, limit)


@app.get("/api/customers/{customer_id}/financial/large-transactions")
def large_transactions(
    customer_id: int,
    threshold: float = Query(default=10000, ge=0),
):
    validate_customer(customer_id)
    return find_large_transactions(customer_id, threshold)


@app.get("/api/customers/{customer_id}/financial/monthly-comparison")
def monthly_comparison(
    customer_id: int,
    year: int = Query(ge=2000, le=2100),
    month: int = Query(ge=1, le=12),
):
    validate_customer(customer_id)
    return compare_monthly_spending(customer_id, year, month)


@app.get("/api/customers/{customer_id}/financial/savings-rate")
def savings_rate(
    customer_id: int,
    year: int = Query(ge=2000, le=2100),
    month: int = Query(ge=1, le=12),
):
    validate_customer(customer_id)
    return calculate_savings_rate(customer_id, year, month)


@app.get("/api/customers/{customer_id}/financial/unusual-spending")
def unusual_spending(
    customer_id: int,
    year: int = Query(ge=2000, le=2100),
    month: int = Query(ge=1, le=12),
):
    validate_customer(customer_id)
    return detect_unusual_spending(customer_id, year, month)


@app.get("/api/customers/{customer_id}/financial/budget")
def budget_recommendations(
    customer_id: int,
    year: int = Query(ge=2000, le=2100),
    month: int = Query(ge=1, le=12),
):
    validate_customer(customer_id)
    return generate_budget_recommendations(
        customer_id, year, month
    )


@app.get("/api/customers/{customer_id}/financial/advisory")
def financial_advisory(
    customer_id: int,
    year: int = Query(ge=2000, le=2100),
    month: int = Query(ge=1, le=12),
):
    validate_customer(customer_id)
    return generate_financial_advisory_report(
        customer_id, year, month
    )


@app.get("/api/customers/{customer_id}/context")
def customer_context(
    customer_id: int,
    year: int | None = Query(default=None, ge=2000, le=2100),
    month: int | None = Query(default=None, ge=1, le=12),
):
    validate_customer(customer_id)

    try:
        return get_customer_context(customer_id, year, month)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail=str(exc),
        )
