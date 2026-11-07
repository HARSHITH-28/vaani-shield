"""Authenticated routes for the VaaniShield Account Opening workflow.

All endpoints derive customer identity from ``get_authenticated_principal``
and never accept ``customer_id`` or ``customer_key`` from request paths, query
strings, or bodies.
"""

from fastapi import APIRouter, Depends, File, HTTPException, Path, Query, Response, UploadFile, status

from backend.api_models import (
    AccountApplicationDocumentType,
    AccountApplicationStartRequest,
    AccountApplicationSubmitRequest,
    AccountApplicationType,
    AccountApplicationUpdateRequest,
)
from backend.auth import get_authenticated_principal
from services.account_application_service import (
    ApplicationConflict,
    ApplicationError,
    ApplicationNotFound,
    ApplicationValidationError,
    DocumentRejected,
    MAX_DOCUMENT_BYTES,
    delete_document,
    get_application,
    get_application_status,
    get_current_application,
    get_document_status,
    list_account_options,
    list_applications,
    open_document,
    prepare_submission,
    return_to_draft,
    start_application,
    store_document,
    submit_application,
    update_application,
    validate_application,
)
from services.principal import CustomerPrincipal

router = APIRouter(prefix="/api/v1/me/account-applications", tags=["account-applications"])

_APP_REF_PATH = Path(pattern=r"^VSA-[A-F0-9]{16}$", max_length=20)


def _raise_http(exc: Exception):
    if isinstance(exc, PermissionError):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required.",
        ) from None
    if isinstance(exc, ApplicationNotFound):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc) or "Application not found.",
        ) from None
    if isinstance(exc, ApplicationValidationError):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "code": "VALIDATION_FAILED",
                "message": str(exc) or "Invalid application data.",
                "errors": exc.errors,
            },
        ) from None
    if isinstance(exc, DocumentRejected):
        msg = str(exc) or "The uploaded document was rejected."
        code = (
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE
            if "limit" in msg.casefold()
            else status.HTTP_415_UNSUPPORTED_MEDIA_TYPE
            if "only pdf" in msg.casefold() or "file type" in msg.casefold()
            else status.HTTP_400_BAD_REQUEST
        )
        raise HTTPException(
            status_code=code,
            detail={"code": "DOCUMENT_REJECTED", "message": msg},
        ) from None
    if isinstance(exc, ApplicationConflict):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "APPLICATION_CONFLICT", "message": str(exc)},
        ) from None
    if isinstance(exc, ApplicationError):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "APPLICATION_ERROR", "message": str(exc)},
        ) from None
    raise HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail="The account application request could not be completed.",
    ) from None


@router.get("/options")
def get_options_endpoint(
    principal: CustomerPrincipal = Depends(get_authenticated_principal),
):
    del principal
    return list_account_options()


@router.get("")
def list_applications_endpoint(
    limit: int = Query(default=20, ge=1, le=50),
    principal: CustomerPrincipal = Depends(get_authenticated_principal),
):
    try:
        return {"items": list_applications(principal, limit=limit)}
    except Exception as exc:
        _raise_http(exc)


@router.post("", status_code=status.HTTP_201_CREATED)
def start_application_endpoint(
    payload: AccountApplicationStartRequest | None = None,
    principal: CustomerPrincipal = Depends(get_authenticated_principal),
):
    try:
        account_type = payload.account_type if payload is not None else None
        return start_application(principal, account_type)
    except Exception as exc:
        _raise_http(exc)


@router.get("/current")
def get_current_application_endpoint(
    principal: CustomerPrincipal = Depends(get_authenticated_principal),
):
    try:
        return {"application": get_current_application(principal)}
    except ApplicationNotFound:
        return {"application": None}
    except Exception as exc:
        _raise_http(exc)


@router.get("/status")
def get_application_status_endpoint(
    application_ref: str | None = Query(default=None, pattern=r"^VSA-[A-F0-9]{16}$", max_length=20),
    account_type: AccountApplicationType | None = Query(default=None),
    principal: CustomerPrincipal = Depends(get_authenticated_principal),
):
    try:
        return get_application_status(
            principal, application_ref=application_ref, account_type=account_type,
        )
    except Exception as exc:
        _raise_http(exc)


@router.get("/{application_ref}")
def get_application_endpoint(
    application_ref: str = _APP_REF_PATH,
    principal: CustomerPrincipal = Depends(get_authenticated_principal),
):
    try:
        return get_application(principal, application_ref)
    except Exception as exc:
        _raise_http(exc)


@router.patch("/{application_ref}")
def update_application_endpoint(
    payload: AccountApplicationUpdateRequest,
    application_ref: str = _APP_REF_PATH,
    principal: CustomerPrincipal = Depends(get_authenticated_principal),
):
    try:
        changes = payload.model_dump(exclude_unset=True)
        return update_application(principal, application_ref, changes)
    except Exception as exc:
        _raise_http(exc)


@router.post("/{application_ref}/validate")
def validate_application_endpoint(
    application_ref: str = _APP_REF_PATH,
    principal: CustomerPrincipal = Depends(get_authenticated_principal),
):
    try:
        return validate_application(principal, application_ref)
    except Exception as exc:
        _raise_http(exc)


@router.get("/{application_ref}/documents")
def get_documents_endpoint(
    application_ref: str = _APP_REF_PATH,
    principal: CustomerPrincipal = Depends(get_authenticated_principal),
):
    try:
        return get_document_status(principal, application_ref)
    except Exception as exc:
        _raise_http(exc)


@router.get("/{application_ref}/documents/{document_type}")
def get_document_content_endpoint(
    document_type: AccountApplicationDocumentType,
    application_ref: str = _APP_REF_PATH,
    principal: CustomerPrincipal = Depends(get_authenticated_principal),
):
    try:
        content, media_type = open_document(principal, application_ref, document_type)
        return Response(
            content=content,
            media_type=media_type,
            headers={"Cache-Control": "no-store"},
        )
    except Exception as exc:
        _raise_http(exc)


@router.post("/{application_ref}/documents/{document_type}")
async def upload_document_endpoint(
    document_type: AccountApplicationDocumentType,
    file: UploadFile = File(...),
    application_ref: str = _APP_REF_PATH,
    principal: CustomerPrincipal = Depends(get_authenticated_principal),
):
    try:
        content = await file.read(MAX_DOCUMENT_BYTES + 1)
        return store_document(
            principal,
            application_ref,
            document_type,
            content,
            file.content_type,
        )
    except Exception as exc:
        _raise_http(exc)
    finally:
        await file.close()


@router.delete("/{application_ref}/documents/{document_type}")
def delete_document_endpoint(
    document_type: AccountApplicationDocumentType,
    application_ref: str = _APP_REF_PATH,
    principal: CustomerPrincipal = Depends(get_authenticated_principal),
):
    try:
        return delete_document(principal, application_ref, document_type)
    except Exception as exc:
        _raise_http(exc)


@router.post("/{application_ref}/prepare-review")
def prepare_review_endpoint(
    application_ref: str = _APP_REF_PATH,
    principal: CustomerPrincipal = Depends(get_authenticated_principal),
):
    try:
        return prepare_submission(principal, application_ref)
    except Exception as exc:
        _raise_http(exc)


@router.post("/{application_ref}/return-to-draft")
def return_to_draft_endpoint(
    application_ref: str = _APP_REF_PATH,
    principal: CustomerPrincipal = Depends(get_authenticated_principal),
):
    try:
        return return_to_draft(principal, application_ref)
    except Exception as exc:
        _raise_http(exc)


@router.post("/{application_ref}/submit")
def submit_application_endpoint(
    payload: AccountApplicationSubmitRequest,
    application_ref: str = _APP_REF_PATH,
    principal: CustomerPrincipal = Depends(get_authenticated_principal),
):
    try:
        return submit_application(
            principal,
            application_ref,
            confirmed=payload.confirmed,
            confirmation_method=payload.confirmation_method,
            review_digest=payload.review_digest,
        )
    except Exception as exc:
        _raise_http(exc)
