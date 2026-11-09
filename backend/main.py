
from pathlib import Path

from dotenv import load_dotenv

# Load local configuration before importing routers/services that construct the
# assistant provider. Explicit process variables retain precedence.
load_dotenv(dotenv_path=Path(__file__).resolve().parent.parent / ".env", override=False)

import logging

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

from backend.voice_routes import router as voice_router
from backend.agent_routes import router as agent_router
from backend.auth import (
    get_authenticated_principal,
    router as demo_auth_router,
)
from backend.conversation_routes import router as conversation_router
from backend.assistant_routes import router as assistant_router
from backend.assistant_voice_routes import router as assistant_voice_router
from backend.customer_routes import router as customer_router
from backend.scam_routes import router as scam_router
from backend.customer_support_routes import router as customer_support_router
from backend.transaction_risk_routes import router as transaction_risk_router
from backend.investigation_routes import router as investigation_router
from backend.account_application_routes import router as account_application_router
from backend.sms_threat_routes import router as sms_threat_router
from backend.scam_report_routes import router as scam_report_router

from services.banking_service import (
    get_customer_profile_by_key,
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
from services.database import initialize_database
from services.speech_storage import start_cleanup_worker, stop_cleanup_worker
from services.assistant_service import orchestrator
from services.pending_action_service import cleanup_expired_pending_actions
from services.investigation_service import cleanup_expired_investigation_actions
from services.principal import CustomerPrincipal


app = FastAPI(
    title="VaaniShield API",
    description="Synthetic banking and financial intelligence API",
    version="1.0.0",
)

app.include_router(voice_router)
app.include_router(agent_router)
app.include_router(demo_auth_router)
app.include_router(conversation_router)
app.include_router(assistant_router)
app.include_router(assistant_voice_router)
app.include_router(customer_router)
app.include_router(scam_router)
app.include_router(customer_support_router)
app.include_router(transaction_risk_router)
app.include_router(investigation_router)
app.include_router(account_application_router)
app.include_router(sms_threat_router)
app.include_router(scam_report_router)


@app.on_event("startup")
def initialize_database_on_startup():
    """Create or migrate the schema without seeding or resetting data."""
    initialize_database()
    cleanup_expired_pending_actions()
    cleanup_expired_investigation_actions()
    start_cleanup_worker()
    from services.scam_assessment_service import start_transcript_cleanup_worker
    start_transcript_cleanup_worker()
    provider = orchestrator.provider_diagnostics()
    logging.getLogger("uvicorn.error").info(
        "assistant_provider_startup provider=%s model=%s credentials_configured=%s",
        provider["provider"], provider["model"], provider["credential_configured"],
    )


@app.on_event("shutdown")
def stop_audio_cleanup_on_shutdown():
    stop_cleanup_worker()
    from services.scam_assessment_service import stop_transcript_cleanup_worker
    stop_transcript_cleanup_worker()


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
    allow_methods=["GET", "POST", "PATCH", "DELETE"],
    allow_headers=["*"],
)


def validate_customer(
    customer_reference: str,
    principal: CustomerPrincipal,
):
    """Require the legacy path reference to match the authenticated identity."""
    if customer_reference != principal.customer_key:
        raise HTTPException(status_code=403, detail="Customer access denied.")

    profile = get_customer_profile_by_key(principal.customer_key)

    if profile is None:
        raise HTTPException(
            status_code=404,
            detail="Customer not found",
        )

    return profile


def resolve_customer_id(customer_reference: str, principal: CustomerPrincipal):
    """Resolve only the authenticated principal's stable key to its DB ID."""
    return validate_customer(customer_reference, principal)["customer_id"]


def _safe_customer_profile(profile):
    return {
        key: profile[key]
        for key in ("name", "monthly_income")
        if key in profile
    }


def _mask_account_number(value):
    value = str(value or "")
    return f"••••{value[-4:]}" if value else ""


def _safe_accounts(accounts):
    return [
        {**account, "account_number": _mask_account_number(account.get("account_number"))}
        for account in accounts
    ]


def _without_internal_id(rows, id_field):
    return [{key: value for key, value in row.items() if key != id_field} for row in rows]


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


@app.get("/api/customers")
def customers(principal: CustomerPrincipal = Depends(get_authenticated_principal)):
    """Compatibility endpoint returning only the signed-in demo profile."""
    profile = get_customer_profile_by_key(principal.customer_key)
    if profile is None:
        raise HTTPException(status_code=404, detail="Customer not found")
    return [{"name": profile["name"]}]


@app.get("/api/customers/{customer_ref}/profile")
def customer_profile(
    customer_ref: str,
    principal: CustomerPrincipal = Depends(get_authenticated_principal),
):
    return _safe_customer_profile(validate_customer(customer_ref, principal))


@app.get("/api/customers/{customer_ref}/balance")
def account_balance(
    customer_ref: str,
    principal: CustomerPrincipal = Depends(get_authenticated_principal),
):
    customer_id = resolve_customer_id(customer_ref, principal)
    return _safe_accounts(get_account_balance(customer_id))


@app.get("/api/customers/{customer_ref}/transactions")
def recent_transactions(
    customer_ref: str,
    limit: int = Query(default=10, ge=1, le=100),
    principal: CustomerPrincipal = Depends(get_authenticated_principal),
):
    customer_id = resolve_customer_id(customer_ref, principal)
    return _without_internal_id(get_recent_transactions(customer_id, limit), "transaction_id")


# Advanced transaction search
@app.get("/api/customers/{customer_ref}/transactions/search")
def transaction_search(
    customer_ref: str,
    merchant: str | None = None,
    category: str | None = None,
    transaction_type: str | None = None,
    min_amount: float | None = Query(default=None, ge=0),
    max_amount: float | None = Query(default=None, ge=0),
    start_date: str | None = None,
    end_date: str | None = None,
    principal: CustomerPrincipal = Depends(get_authenticated_principal),
):
    customer_id = resolve_customer_id(customer_ref, principal)

    if min_amount is not None and max_amount is not None:
        if min_amount > max_amount:
            raise HTTPException(
                status_code=400,
                detail="min_amount cannot exceed max_amount"
            )

    results = search_transactions(
        customer_id,
        merchant=merchant,
        category=category,
        transaction_type=transaction_type,
        min_amount=min_amount,
        max_amount=max_amount,
        start_date=start_date,
        end_date=end_date,
    )
    return _without_internal_id(results, "transaction_id")


@app.get("/api/customers/{customer_ref}/cards")
def cards(
    customer_ref: str,
    principal: CustomerPrincipal = Depends(get_authenticated_principal),
):
    customer_id = resolve_customer_id(customer_ref, principal)
    return _without_internal_id(get_card_details(customer_id), "card_id")


@app.get("/api/customers/{customer_ref}/loans")
def loans(
    customer_ref: str,
    principal: CustomerPrincipal = Depends(get_authenticated_principal),
):
    customer_id = resolve_customer_id(customer_ref, principal)
    return _without_internal_id(get_loan_details(customer_id), "loan_id")


@app.get("/api/customers/{customer_ref}/financial/summary")
def financial_summary(customer_ref: str, principal: CustomerPrincipal = Depends(get_authenticated_principal)):
    customer_id = resolve_customer_id(customer_ref, principal)
    return get_spending_summary(customer_id)


@app.get("/api/customers/{customer_ref}/financial/categories")
def financial_categories(customer_ref: str, principal: CustomerPrincipal = Depends(get_authenticated_principal)):
    customer_id = resolve_customer_id(customer_ref, principal)
    return get_category_breakdown(customer_id)


@app.get("/api/customers/{customer_ref}/financial/merchants")
def financial_merchants(
    customer_ref: str,
    limit: int = Query(default=5, ge=1, le=50),
    principal: CustomerPrincipal = Depends(get_authenticated_principal),
):
    customer_id = resolve_customer_id(customer_ref, principal)
    return get_top_merchants(customer_id, limit)


@app.get("/api/customers/{customer_ref}/financial/large-transactions")
def large_transactions(
    customer_ref: str,
    threshold: float = Query(default=10000, ge=0),
    principal: CustomerPrincipal = Depends(get_authenticated_principal),
):
    customer_id = resolve_customer_id(customer_ref, principal)
    return _without_internal_id(
        find_large_transactions(customer_id, threshold), "transaction_id"
    )


@app.get("/api/customers/{customer_ref}/financial/monthly-comparison")
def monthly_comparison(
    customer_ref: str,
    year: int = Query(ge=2000, le=2100),
    month: int = Query(ge=1, le=12),
    principal: CustomerPrincipal = Depends(get_authenticated_principal),
):
    customer_id = resolve_customer_id(customer_ref, principal)
    return compare_monthly_spending(customer_id, year, month)


@app.get("/api/customers/{customer_ref}/financial/savings-rate")
def savings_rate(
    customer_ref: str,
    year: int = Query(ge=2000, le=2100),
    month: int = Query(ge=1, le=12),
    principal: CustomerPrincipal = Depends(get_authenticated_principal),
):
    customer_id = resolve_customer_id(customer_ref, principal)
    return calculate_savings_rate(customer_id, year, month)


@app.get("/api/customers/{customer_ref}/financial/unusual-spending")
def unusual_spending(
    customer_ref: str,
    year: int = Query(ge=2000, le=2100),
    month: int = Query(ge=1, le=12),
    principal: CustomerPrincipal = Depends(get_authenticated_principal),
):
    customer_id = resolve_customer_id(customer_ref, principal)
    return detect_unusual_spending(customer_id, year, month)


@app.get("/api/customers/{customer_ref}/financial/budget")
def budget_recommendations(
    customer_ref: str,
    year: int = Query(ge=2000, le=2100),
    month: int = Query(ge=1, le=12),
    principal: CustomerPrincipal = Depends(get_authenticated_principal),
):
    customer_id = resolve_customer_id(customer_ref, principal)
    return generate_budget_recommendations(
        customer_id, year, month
    )


@app.get("/api/customers/{customer_ref}/financial/advisory")
def financial_advisory(
    customer_ref: str,
    year: int = Query(ge=2000, le=2100),
    month: int = Query(ge=1, le=12),
    principal: CustomerPrincipal = Depends(get_authenticated_principal),
):
    customer_id = resolve_customer_id(customer_ref, principal)
    return generate_financial_advisory_report(
        customer_id, year, month
    )


@app.get("/api/customers/{customer_ref}/context")
def customer_context(
    customer_ref: str,
    year: int | None = Query(default=None, ge=2000, le=2100),
    month: int | None = Query(default=None, ge=1, le=12),
    principal: CustomerPrincipal = Depends(get_authenticated_principal),
):
    resolve_customer_id(customer_ref, principal)

    try:
        context = get_customer_context(principal.customer_key, year, month)
        context["customer"] = _safe_customer_profile(context["customer"])
        context["banking"]["account_balance"] = _safe_accounts(
            context["banking"]["account_balance"]
        )
        context["banking"]["recent_transactions"] = _without_internal_id(
            context["banking"]["recent_transactions"], "transaction_id"
        )
        context["banking"]["cards"] = _without_internal_id(
            context["banking"]["cards"], "card_id"
        )
        context["banking"]["loans"] = _without_internal_id(
            context["banking"]["loans"], "loan_id"
        )
        context["metadata"].pop("customer_id", None)
        context["metadata"].pop("customer_key", None)
        return context
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail=str(exc),
        )
