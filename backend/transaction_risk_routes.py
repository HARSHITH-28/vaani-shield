"""Authenticated API for deterministic transaction-risk assessments."""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status

from backend.api_models import (
    CursorPage,
    TransactionRiskAssessmentDetail,
    TransactionRiskAssessmentRequest,
    TransactionRiskAssessmentResponse,
    TransactionRiskAssessmentSummary,
)
from backend.auth import get_authenticated_principal
from services.principal import CustomerPrincipal
from services.transaction_risk_persistence import (
    InvalidTransactionRiskRequest,
    TransactionRiskAssessmentError,
    TransactionRiskAssessmentNotFound,
    TransactionRiskTransactionNotFound,
    create_transaction_assessment,
    get_transaction_assessment,
    list_transaction_assessments,
)


router = APIRouter(prefix="/api/v1/me/scam", tags=["Transaction Risk"])


@router.post(
    "/assessments/transactions",
    response_model=TransactionRiskAssessmentResponse,
    status_code=status.HTTP_201_CREATED,
)
def post_transaction_assessment(
    payload: TransactionRiskAssessmentRequest,
    response: Response,
    principal: CustomerPrincipal = Depends(get_authenticated_principal),
):
    try:
        result = create_transaction_assessment(principal, payload.transaction_ref)
        if result["reused"]:
            response.status_code = status.HTTP_200_OK
        return result
    except TransactionRiskTransactionNotFound:
        # Same response for absent and cross-customer references.
        raise HTTPException(status_code=404, detail="Transaction not found.") from None
    except InvalidTransactionRiskRequest as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    except TransactionRiskAssessmentError:
        raise HTTPException(
            status_code=503,
            detail="Transaction-risk assessment is temporarily unavailable.",
        ) from None


@router.get(
    "/transaction-assessments",
    response_model=CursorPage[TransactionRiskAssessmentSummary],
)
def get_transaction_assessments(
    limit: int = Query(default=25, ge=1, le=100),
    cursor: str | None = Query(default=None, max_length=512),
    principal: CustomerPrincipal = Depends(get_authenticated_principal),
):
    try:
        return list_transaction_assessments(principal, limit, cursor)
    except InvalidTransactionRiskRequest:
        raise HTTPException(status_code=400, detail="Invalid pagination cursor.") from None


@router.get(
    "/transaction-assessments/{assessment_id}",
    response_model=TransactionRiskAssessmentDetail,
)
def get_transaction_assessment_detail(
    assessment_id: UUID,
    principal: CustomerPrincipal = Depends(get_authenticated_principal),
):
    try:
        return get_transaction_assessment(principal, assessment_id)
    except TransactionRiskAssessmentNotFound:
        raise HTTPException(status_code=404, detail="Assessment not found.") from None
