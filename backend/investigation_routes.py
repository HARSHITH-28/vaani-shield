"""Authenticated APIs for customer-owned synthetic security investigations."""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status

from backend.api_models import (
    CursorPage, InvestigationCloseRequest, InvestigationCreateRequest,
    InvestigationDetail, InvestigationEvidenceLinkRequest,
    InvestigationEvidenceReference, InvestigationNoteRequest,
    InvestigationStatusUpdateRequest, InvestigationSummary,
)
from backend.auth import get_authenticated_principal
from services.investigation_service import (
    InvestigationConflict, InvestigationError, InvestigationEvidenceNotFound,
    InvestigationNotFound, add_investigation_note, close_investigation,
    create_investigation, get_investigation, link_investigation_evidence,
    list_investigations, update_investigation_status,
)
from services.principal import CustomerPrincipal


router = APIRouter(prefix="/api/v1/me/scam/investigations", tags=["Investigations"])


def _raise_service_error(exc):
    if isinstance(exc, PermissionError):
        raise HTTPException(status_code=401, detail="Authentication required.") from None
    if isinstance(exc, InvestigationNotFound):
        raise HTTPException(status_code=404, detail="Investigation not found.") from None
    if isinstance(exc, InvestigationEvidenceNotFound):
        raise HTTPException(status_code=404, detail="Assessment evidence not found.") from None
    if isinstance(exc, InvestigationConflict):
        raise HTTPException(status_code=409, detail=str(exc)) from None
    if isinstance(exc, InvestigationError):
        raise HTTPException(status_code=422, detail=str(exc)) from None
    raise exc


@router.post("", response_model=InvestigationDetail, status_code=status.HTTP_201_CREATED)
def post_investigation(
    payload: InvestigationCreateRequest,
    principal: CustomerPrincipal = Depends(get_authenticated_principal),
):
    try:
        return create_investigation(
            principal, title=payload.title, category=payload.category,
            summary=payload.summary, evidence=payload.evidence,
        )
    except (InvestigationNotFound, InvestigationEvidenceNotFound, InvestigationConflict, InvestigationError) as exc:
        _raise_service_error(exc)


@router.get("", response_model=CursorPage[InvestigationSummary])
def get_investigations(
    limit: int = Query(default=25, ge=1, le=100),
    cursor: str | None = Query(default=None, max_length=512),
    status_filter: str | None = Query(default=None, alias="status"),
    category: str | None = Query(default=None),
    search: str | None = Query(default=None, max_length=120),
    principal: CustomerPrincipal = Depends(get_authenticated_principal),
):
    try:
        return list_investigations(
            principal, limit=limit, cursor=cursor, status=status_filter,
            category=category, search=search,
        )
    except (InvestigationError, InvestigationConflict) as exc:
        _raise_service_error(exc)


@router.get("/{investigation_ref}", response_model=InvestigationDetail)
def get_investigation_detail(
    investigation_ref: str,
    principal: CustomerPrincipal = Depends(get_authenticated_principal),
):
    try:
        return get_investigation(principal, investigation_ref)
    except InvestigationNotFound as exc:
        _raise_service_error(exc)


@router.post("/{investigation_ref}/evidence", response_model=InvestigationDetail)
def post_investigation_evidence(
    investigation_ref: str,
    payload: InvestigationEvidenceLinkRequest,
    principal: CustomerPrincipal = Depends(get_authenticated_principal),
):
    try:
        return link_investigation_evidence(principal, investigation_ref, payload.evidence)
    except (InvestigationNotFound, InvestigationEvidenceNotFound, InvestigationConflict, InvestigationError) as exc:
        _raise_service_error(exc)


@router.post("/{investigation_ref}/notes", response_model=InvestigationDetail)
def post_investigation_note(
    investigation_ref: str,
    payload: InvestigationNoteRequest,
    principal: CustomerPrincipal = Depends(get_authenticated_principal),
):
    try:
        return add_investigation_note(principal, investigation_ref, payload.note)
    except (InvestigationNotFound, InvestigationConflict, InvestigationError) as exc:
        _raise_service_error(exc)


@router.patch("/{investigation_ref}/status", response_model=InvestigationDetail)
def patch_investigation_status(
    investigation_ref: str,
    payload: InvestigationStatusUpdateRequest,
    principal: CustomerPrincipal = Depends(get_authenticated_principal),
):
    try:
        return update_investigation_status(principal, investigation_ref, payload.status)
    except (InvestigationNotFound, InvestigationConflict, InvestigationError) as exc:
        _raise_service_error(exc)


@router.post("/{investigation_ref}/close", response_model=InvestigationDetail)
def post_investigation_close(
    investigation_ref: str,
    payload: InvestigationCloseRequest,
    principal: CustomerPrincipal = Depends(get_authenticated_principal),
):
    try:
        return close_investigation(principal, investigation_ref)
    except (InvestigationNotFound, InvestigationConflict, InvestigationError) as exc:
        _raise_service_error(exc)
