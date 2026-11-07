"""Authenticated conversation persistence endpoints."""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status

from backend.api_models import (
    ConversationCreateRequest,
    ConversationDetail,
    ConversationSummary,
    ConversationTurn,
    CursorPage,
)
from backend.auth import get_authenticated_principal
from services.conversation_service import (
    ConversationNotFoundError,
    InvalidCursorError,
    create_conversation,
    delete_conversation_turn,
    get_conversation,
    get_conversation_history,
    list_conversations,
)
from services.principal import CustomerPrincipal


router = APIRouter(prefix="/api/v1/assistant/conversations", tags=["Assistant Conversations"])


@router.delete("/{conversation_id}/turns/{turn_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_turn(
    conversation_id: UUID,
    turn_id: UUID,
    principal: CustomerPrincipal = Depends(get_authenticated_principal),
):
    try:
        delete_conversation_turn(principal, conversation_id, turn_id)
    except ConversationNotFoundError:
        raise HTTPException(status_code=404, detail="Conversation message not found.") from None
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("", response_model=ConversationDetail, status_code=status.HTTP_201_CREATED)
def post_conversation(
    payload: ConversationCreateRequest,
    principal: CustomerPrincipal = Depends(get_authenticated_principal),
):
    return create_conversation(principal, payload.title)


@router.get("", response_model=CursorPage[ConversationSummary])
def get_conversations(
    limit: int = Query(default=25, ge=1, le=100),
    cursor: str | None = Query(default=None, max_length=512),
    principal: CustomerPrincipal = Depends(get_authenticated_principal),
):
    try:
        return list_conversations(principal, limit=limit, cursor=cursor)
    except InvalidCursorError:
        raise HTTPException(status_code=400, detail="Invalid pagination cursor.") from None


@router.get("/{conversation_id}", response_model=ConversationDetail)
def get_conversation_detail(
    conversation_id: UUID,
    principal: CustomerPrincipal = Depends(get_authenticated_principal),
):
    try:
        return get_conversation(principal, conversation_id)
    except ConversationNotFoundError:
        raise HTTPException(status_code=404, detail="Conversation not found.") from None


@router.get("/{conversation_id}/turns", response_model=CursorPage[ConversationTurn])
def get_conversation_turns(
    conversation_id: UUID,
    limit: int = Query(default=25, ge=1, le=100),
    cursor: str | None = Query(default=None, max_length=512),
    principal: CustomerPrincipal = Depends(get_authenticated_principal),
):
    try:
        return get_conversation_history(principal, conversation_id, limit, cursor)
    except ConversationNotFoundError:
        raise HTTPException(status_code=404, detail="Conversation not found.") from None
    except InvalidCursorError:
        raise HTTPException(status_code=400, detail="Invalid pagination cursor.") from None
