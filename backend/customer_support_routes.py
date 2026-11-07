"""Authenticated customer self-service APIs for synthetic support cases."""

from fastapi import APIRouter, Depends, HTTPException, Query, status

from backend.api_models import (
    CursorPage,
    SupportTicketCreateRequest,
    SupportTicketDetail,
    SupportTicketStatus,
    SupportTicketStatusEvent,
    SupportTicketStatusUpdateRequest,
    SupportTicketSummary,
)
from backend.auth import get_authenticated_principal
from services.customer_support_service import (
    InvalidSupportCaseTransition,
    InvalidSupportCursor,
    SupportCaseError,
    SupportCaseNotFoundError,
    close_support_case,
    create_support_case,
    get_support_case,
    get_support_case_events,
    list_support_cases,
)
from services.principal import CustomerPrincipal


router = APIRouter(prefix="/api/v1/me/support/tickets", tags=["Customer Support"])


@router.post("", response_model=SupportTicketDetail, status_code=status.HTTP_201_CREATED)
def post_support_ticket(
    payload: SupportTicketCreateRequest,
    principal: CustomerPrincipal = Depends(get_authenticated_principal),
):
    try:
        # Origin and synthetic status are set by the service, never by the body.
        return create_support_case(
            principal,
            category=payload.category,
            priority=payload.priority,
            subject=payload.subject,
            description=payload.description,
            conversation_id=str(payload.conversation_id) if payload.conversation_id else None,
            language=payload.language,
        )
    except PermissionError:
        raise HTTPException(status_code=401, detail="Authentication required.") from None
    except SupportCaseNotFoundError:
        raise HTTPException(status_code=404, detail="Support resource not found.") from None
    except SupportCaseError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None


@router.get("", response_model=CursorPage[SupportTicketSummary])
def get_support_tickets(
    limit: int = Query(default=25, ge=1, le=100),
    cursor: str | None = Query(default=None, max_length=64),
    ticket_status: SupportTicketStatus | None = Query(default=None, alias="status"),
    category: str | None = Query(default=None, max_length=32),
    priority: str | None = Query(default=None, max_length=16),
    principal: CustomerPrincipal = Depends(get_authenticated_principal),
):
    try:
        return list_support_cases(
            principal,
            limit=limit,
            cursor=cursor,
            status=ticket_status,
            category=category,
            priority=priority,
        )
    except PermissionError:
        raise HTTPException(status_code=401, detail="Authentication required.") from None
    except InvalidSupportCursor:
        raise HTTPException(status_code=400, detail="Invalid pagination cursor.") from None
    except SupportCaseError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None


@router.get("/{ticket_ref}/events", response_model=list[SupportTicketStatusEvent])
def get_support_ticket_events(
    ticket_ref: str,
    principal: CustomerPrincipal = Depends(get_authenticated_principal),
):
    try:
        return get_support_case_events(principal, ticket_ref)
    except PermissionError:
        raise HTTPException(status_code=401, detail="Authentication required.") from None
    except SupportCaseNotFoundError:
        raise HTTPException(status_code=404, detail="Support ticket not found.") from None


@router.get("/{ticket_ref}", response_model=SupportTicketDetail)
def get_support_ticket(
    ticket_ref: str,
    principal: CustomerPrincipal = Depends(get_authenticated_principal),
):
    try:
        return get_support_case(principal, ticket_ref)
    except PermissionError:
        raise HTTPException(status_code=401, detail="Authentication required.") from None
    except SupportCaseNotFoundError:
        raise HTTPException(status_code=404, detail="Support ticket not found.") from None


@router.patch("/{ticket_ref}/status", response_model=SupportTicketDetail)
def patch_support_ticket_status(
    ticket_ref: str,
    payload: SupportTicketStatusUpdateRequest,
    principal: CustomerPrincipal = Depends(get_authenticated_principal),
):
    # The request contract requires confirmed=true and a button confirmation.
    # Only customer closure is implemented; staff-only states are not accepted.
    try:
        return close_support_case(principal, ticket_ref)
    except PermissionError:
        raise HTTPException(status_code=401, detail="Authentication required.") from None
    except SupportCaseNotFoundError:
        raise HTTPException(status_code=404, detail="Support ticket not found.") from None
    except InvalidSupportCaseTransition as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None

