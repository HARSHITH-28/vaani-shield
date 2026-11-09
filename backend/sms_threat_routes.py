"""FastAPI routes for Android companion pairing and SMS Threat Intelligence (Phase 2).

Transport Security Notice:
- Production deployments MUST enforce HTTPS (TLS 1.2+) so device bearer tokens
  and SMS notification payloads are never transmitted in cleartext over public networks.
- Development HTTP is permissible ONLY on an isolated, trusted private LAN during
  local testing and must never be exposed to the public internet.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request, Response, status

from backend.api_models import (
    CursorPage,
    DeviceHeartbeatRequest,
    DeviceHeartbeatResponse,
    DevicePairRequest,
    DevicePairResponse,
    DeviceStatusUpdateRequest,
    DeviceTokenRotateResponse,
    PairedDeviceListResponse,
    PairedDeviceSummary,
    PairTokenCreateRequest,
    PairTokenCreateResponse,
    SmsIngestRequest,
    SmsIngestResponse,
    SmsRiskLevel,
    SmsThreatDetail,
    SmsThreatSummary,
)
from backend.auth import get_authenticated_principal
from services.principal import CustomerPrincipal, DevicePrincipal
from services.sms_threat_service import (
    DeviceAccessForbiddenError,
    DeviceAuthenticationError,
    DeviceConflictError,
    DeviceNotFoundError,
    DeviceStateConflictError,
    InvalidSmsInputError,
    PairingCodeExpiredError,
    PairingCodeInvalidError,
    PairingCodeReusedError,
    RateLimitExceededError,
    SmsThreatNotFoundError,
    acknowledge_sms_threat,
    authenticate_device_token,
    create_pairing_code,
    get_sms_threat,
    ingest_sms_event,
    list_customer_devices,
    list_sms_threats,
    pair_device,
    record_device_heartbeat,
    rotate_customer_device_token,
    update_customer_device_status,
)


router = APIRouter(tags=["SMS Threat Intelligence"])


def get_authenticated_device(request: Request) -> DevicePrincipal:
    """Authenticate an Android companion request via Authorization: Bearer <device_token>.

    Never accepts banking cookies and never grants CustomerPrincipal access.
    """
    auth_header = request.headers.get("authorization", "")
    scheme, _, token = auth_header.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid device authentication header.",
        )
    try:
        return authenticate_device_token(token)
    except DeviceAuthenticationError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(exc),
        ) from None
    except DeviceAccessForbiddenError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=str(exc),
        ) from None


@router.post(
    "/api/v1/me/scam/devices/pair-token",
    response_model=PairTokenCreateResponse,
    status_code=status.HTTP_201_CREATED,
)
def post_create_pairing_code(
    payload: PairTokenCreateRequest | None = Body(default=None),
    principal: CustomerPrincipal = Depends(get_authenticated_principal),
):
    try:
        device_name = payload.device_name if payload is not None else None
        return create_pairing_code(principal, device_name=device_name)
    except RateLimitExceededError as exc:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=str(exc),
        ) from None


@router.get(
    "/api/v1/me/scam/devices",
    response_model=PairedDeviceListResponse,
)
def get_customer_devices(
    principal: CustomerPrincipal = Depends(get_authenticated_principal),
):
    return list_customer_devices(principal)


@router.post(
    "/api/v1/me/scam/devices/{device_id}/status",
    response_model=PairedDeviceSummary,
)
def post_update_device_status(
    device_id: str,
    payload: DeviceStatusUpdateRequest,
    principal: CustomerPrincipal = Depends(get_authenticated_principal),
):
    try:
        return update_customer_device_status(principal, device_id, payload.status)
    except DeviceNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from None
    except DeviceStateConflictError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from None
    except InvalidSmsInputError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from None


@router.post(
    "/api/v1/me/scam/devices/{device_id}/rotate-token",
    response_model=DeviceTokenRotateResponse,
)
def post_rotate_device_token(
    device_id: str,
    principal: CustomerPrincipal = Depends(get_authenticated_principal),
):
    try:
        return rotate_customer_device_token(principal, device_id)
    except DeviceNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from None
    except DeviceStateConflictError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from None


@router.get(
    "/api/v1/me/scam/sms-threats",
    response_model=CursorPage[SmsThreatSummary],
)
def get_customer_sms_threats(
    limit: int = Query(default=25, ge=1, le=100),
    cursor: str | None = Query(default=None, max_length=512),
    risk_level: SmsRiskLevel | None = Query(default=None),
    acknowledged: bool | None = Query(default=None),
    device_id: str | None = Query(default=None, max_length=128),
    principal: CustomerPrincipal = Depends(get_authenticated_principal),
):
    try:
        return list_sms_threats(
            principal,
            limit=limit,
            cursor=cursor,
            risk_level=risk_level,
            acknowledged=acknowledged,
            device_id=device_id,
        )
    except InvalidSmsInputError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from None


@router.get(
    "/api/v1/me/scam/sms-threats/{sms_event_id}",
    response_model=SmsThreatDetail,
)
def get_customer_sms_threat_detail(
    sms_event_id: UUID,
    principal: CustomerPrincipal = Depends(get_authenticated_principal),
):
    try:
        return get_sms_threat(principal, sms_event_id)
    except SmsThreatNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from None


@router.post(
    "/api/v1/me/scam/sms-threats/{sms_event_id}/acknowledge",
    response_model=SmsThreatDetail,
)
def post_acknowledge_customer_sms_threat(
    sms_event_id: UUID,
    principal: CustomerPrincipal = Depends(get_authenticated_principal),
):
    try:
        return acknowledge_sms_threat(principal, sms_event_id)
    except SmsThreatNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from None


@router.post(
    "/api/v1/devices/pair",
    response_model=DevicePairResponse,
    status_code=status.HTTP_201_CREATED,
)
def post_pair_device(
    payload: DevicePairRequest,
    request: Request,
):
    client_ip = request.client.host if request.client else "127.0.0.1"
    try:
        return pair_device(
            pairing_code=payload.pairing_code,
            device_id=payload.device_id,
            device_name=payload.device_name,
            device_model=payload.device_model,
            android_version=payload.android_version,
            app_version=payload.app_version,
            client_key=client_ip,
        )
    except RateLimitExceededError as exc:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=str(exc),
        ) from None
    except PairingCodeInvalidError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(exc),
        ) from None
    except PairingCodeExpiredError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from None
    except (PairingCodeReusedError, DeviceConflictError) as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from None
    except InvalidSmsInputError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from None


@router.post(
    "/api/v1/devices/heartbeat",
    response_model=DeviceHeartbeatResponse,
)
def post_device_heartbeat(
    payload: DeviceHeartbeatRequest | None = Body(default=None),
    device_principal: DevicePrincipal = Depends(get_authenticated_device),
):
    try:
        app_version = payload.app_version if payload is not None else None
        return record_device_heartbeat(device_principal, app_version=app_version)
    except DeviceAuthenticationError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(exc),
        ) from None
    except DeviceAccessForbiddenError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=str(exc),
        ) from None


@router.post(
    "/api/v1/devices/sms/ingest",
    response_model=SmsIngestResponse,
    status_code=status.HTTP_201_CREATED,
)
def post_ingest_sms(
    payload: SmsIngestRequest,
    response: Response,
    device_principal: DevicePrincipal = Depends(get_authenticated_device),
):
    try:
        result = ingest_sms_event(
            device_principal,
            source_package=payload.source_package,
            sender=payload.sender,
            sender_type=payload.sender_type,
            message_text=payload.message_text,
            message_timestamp=payload.message_timestamp,
            client_dedup_key=payload.client_dedup_key,
        )
        if result.get("deduplicated"):
            response.status_code = status.HTTP_200_OK
        return result
    except InvalidSmsInputError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from None
    except DeviceAuthenticationError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(exc),
        ) from None
    except DeviceAccessForbiddenError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=str(exc),
        ) from None

