"""Authenticated unified Analysis Reports read API."""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query

from backend.api_models import ScamReport, ScamReportPage
from backend.auth import get_authenticated_principal
from services.principal import CustomerPrincipal
from services.scam_assessment_service import AssessmentNotFoundError, cleanup_expired_transcripts
from services.sms_threat_service import SmsThreatNotFoundError
from services.scam_report_service import InvalidReportQuery, get_report, list_reports


router = APIRouter(prefix="/api/v1/me/scam/reports", tags=["Scam Analysis Reports"])


@router.get("", response_model=ScamReportPage)
def get_reports(
    limit: int = Query(default=25, ge=1, le=100),
    cursor: str | None = Query(default=None, max_length=512),
    source_type: str | None = Query(default=None, max_length=32),
    risk_level: str | None = Query(default=None, max_length=16),
    date_from: str | None = Query(default=None, alias="from", max_length=40),
    date_to: str | None = Query(default=None, alias="to", max_length=40),
    search: str | None = Query(default=None, max_length=200),
    principal: CustomerPrincipal = Depends(get_authenticated_principal),
):
    try:
        cleanup_expired_transcripts()
        return list_reports(principal, limit=limit, cursor=cursor, source_type=source_type,
                            risk_level=risk_level, date_from=date_from, date_to=date_to, search=search)
    except InvalidReportQuery as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None


@router.get("/{source_type}/{report_id}", response_model=ScamReport)
def get_report_detail(
    source_type: str,
    report_id: UUID,
    principal: CustomerPrincipal = Depends(get_authenticated_principal),
):
    try:
        cleanup_expired_transcripts()
        return get_report(principal, source_type, report_id)
    except (AssessmentNotFoundError, SmsThreatNotFoundError):
        raise HTTPException(status_code=404, detail="Report not found.") from None
    except InvalidReportQuery as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
