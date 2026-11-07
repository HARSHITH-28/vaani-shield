"""Authenticated self-service scam message assessment API."""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status

from backend.api_models import (
    CursorPage,
    ScamCallAssessmentRequest,
    ScamAssessmentDetail,
    ScamAssessmentSummary,
    ScamMessageAssessmentCreate,
    ScamVoiceAssessmentResponse,
)
from backend.auth import get_authenticated_principal
from services.principal import CustomerPrincipal
from services.scam_assessment_service import (
    AssessmentEngineError,
    AssessmentNotFoundError,
    InvalidAssessmentError,
    ScamTranscriptionError,
    create_assessment,
    create_voice_assessment,
    get_assessment,
    list_assessments,
)


router = APIRouter(prefix="/api/v1/me/scam/assessments", tags=["Scam Detection"])


@router.post("/messages", response_model=ScamAssessmentDetail, status_code=status.HTTP_201_CREATED)
def post_message_assessment(
    payload: ScamMessageAssessmentCreate,
    principal: CustomerPrincipal = Depends(get_authenticated_principal),
):
    try:
        return create_assessment(principal, payload.message_text, payload.language_hint)
    except InvalidAssessmentError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    except AssessmentEngineError:
        raise HTTPException(
            status_code=503,
            detail="Scam assessment is temporarily unavailable.",
        ) from None


@router.post("/calls", response_model=ScamVoiceAssessmentResponse, status_code=status.HTTP_201_CREATED)
def post_call_assessment(
    payload: ScamCallAssessmentRequest,
    principal: CustomerPrincipal = Depends(get_authenticated_principal),
):
    try:
        return create_voice_assessment(
            principal, payload.audio_upload_id, payload.language_hint
        )
    except InvalidAssessmentError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    except ScamTranscriptionError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from None
    except AssessmentEngineError:
        raise HTTPException(
            status_code=503,
            detail="Scam assessment is temporarily unavailable. The transcript was not retained.",
        ) from None


@router.get("", response_model=CursorPage[ScamAssessmentSummary])
def get_assessments(
    limit: int = Query(default=25, ge=1, le=100),
    cursor: str | None = Query(default=None, max_length=512),
    principal: CustomerPrincipal = Depends(get_authenticated_principal),
):
    try:
        return list_assessments(principal, limit, cursor)
    except InvalidAssessmentError:
        raise HTTPException(status_code=400, detail="Invalid pagination cursor.") from None


@router.get("/{assessment_id}", response_model=ScamAssessmentDetail)
def get_assessment_detail(
    assessment_id: UUID,
    principal: CustomerPrincipal = Depends(get_authenticated_principal),
):
    try:
        return get_assessment(principal, assessment_id)
    except AssessmentNotFoundError:
        raise HTTPException(status_code=404, detail="Assessment not found.") from None
