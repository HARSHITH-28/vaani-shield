"""Customer-scoped unified read model for persisted scam reports."""

from __future__ import annotations

import base64
from datetime import date, datetime, time, timezone
import json
import sqlite3
from uuid import UUID

from services import database
from services.principal import CustomerPrincipal
from services.scam_assessment_service import _owner, AssessmentNotFoundError
from services.sms_threat_service import SmsThreatNotFoundError


SOURCES = {"MANUAL_MESSAGE", "INCOMING_SMS", "VOICE_CALL"}
RISKS = {"CRITICAL", "HIGH", "MEDIUM", "LOW", "UNKNOWN"}


class InvalidReportQuery(ValueError):
    pass


def _parse_cursor(cursor: str | None) -> tuple[str, str, str] | None:
    if cursor is None:
        return None
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        value = json.loads(raw)
        if not isinstance(value, list) or len(value) != 3:
            raise ValueError
        timestamp, source, report_id = value
        datetime.fromisoformat(timestamp)
        if source not in SOURCES:
            raise ValueError
        UUID(report_id)
        return timestamp, source, report_id
    except (ValueError, TypeError, UnicodeError, json.JSONDecodeError):
        raise InvalidReportQuery("Invalid pagination cursor.") from None


def _make_cursor(item: dict) -> str:
    data = json.dumps([item["analysis_timestamp"], item["source_type"], item["report_id"]], separators=(",", ":"))
    return base64.urlsafe_b64encode(data.encode()).rstrip(b"=").decode()


def _assessment_item(row: sqlite3.Row, now: str) -> dict:
    call = row["source_mode"] == "voice_call"
    available = call and row["transcript_status"] == "AVAILABLE" and row["transcript_expires_at"] and row["transcript_expires_at"] > now
    return {
        "report_id": row["assessment_id"],
        "source_type": "VOICE_CALL" if call else "MANUAL_MESSAGE",
        "risk_level": row["risk_level"], "risk_score": None,
        "analysis_timestamp": row["created_at"],
        "indicators": json.loads(row["indicators_json"]),
        "explanation": row["explanation"], "warning": row["warning"],
        "recommended_actions": json.loads(row["recommended_steps_json"]),
        "detected_languages": json.loads(row["detected_languages_json"]),
        "language_status": row["language_status"],
        "is_code_mixed": bool(row["is_code_mixed"]),
        "engine_version": row["engine_version"],
        "redacted_message_preview": None,
        "redacted_call_transcript": row["redacted_transcript"] if available else None,
        "transcript_expires_at": row["transcript_expires_at"] if available else None,
        "content_availability": (
            "REDACTED_TRANSCRIPT" if available else
            "TRANSCRIPT_EXPIRED" if call and row["transcript_status"] == "EXPIRED" else
            "SOURCE_CONTENT_NOT_STORED"
        ),
        "sender": None, "source_package": None, "device_id": None,
    }


def _sms_item(row: sqlite3.Row) -> dict:
    return {
        "report_id": row["sms_event_id"], "source_type": "INCOMING_SMS",
        "risk_level": row["risk_level"], "risk_score": None,
        "analysis_timestamp": row["received_at"],
        "indicators": json.loads(row["indicators_json"]),
        "explanation": row["explanation"], "warning": row["warning"],
        "recommended_actions": json.loads(row["recommendations_json"]),
        "detected_languages": json.loads(row["detected_languages_json"]),
        "language_status": row["language_status"], "is_code_mixed": bool(row["code_mixed"]),
        "engine_version": row["engine_version"],
        "redacted_message_preview": row["redacted_message"],
        "redacted_call_transcript": None, "transcript_expires_at": None,
        "content_availability": "REDACTED_SMS_PREVIEW",
        "sender": row["sender"], "source_package": row["source_package"],
        "device_id": row["device_id"],
    }


def list_reports(principal: CustomerPrincipal, *, limit=25, cursor=None,
                 source_type=None, risk_level=None, date_from=None, date_to=None, search=None):
    owner = _owner(principal)
    if not 1 <= limit <= 100:
        raise InvalidReportQuery("limit must be between 1 and 100.")
    source_type = source_type.upper() if source_type else None
    risk_level = risk_level.upper() if risk_level else None
    if source_type and source_type not in SOURCES:
        raise InvalidReportQuery("Unsupported source_type filter.")
    if risk_level and risk_level not in RISKS:
        raise InvalidReportQuery("Unsupported risk_level filter.")
    try:
        start = _date_bound(date_from, end=False) if date_from else None
        end = _date_bound(date_to, end=True) if date_to else None
    except (ValueError, TypeError):
        raise InvalidReportQuery("Invalid date filter.") from None
    if start and end and start > end:
        raise InvalidReportQuery("from must not be after to.")
    after = _parse_cursor(cursor)
    needle = search.strip().casefold() if search else ""
    if search is not None and (not needle or len(needle) > 200):
        raise InvalidReportQuery("search must contain 1 to 200 characters.")

    conn = database.get_connection()
    try:
        assessment_rows = conn.execute(
            "SELECT assessment_id, source_mode, risk_level, indicators_json, explanation, warning, "
            "recommended_steps_json, detected_languages_json, is_code_mixed, language_status, "
            "engine_version, created_at, redacted_transcript, transcript_expires_at, transcript_status "
            "FROM scam_assessments WHERE customer_key = ?", (owner,)
        ).fetchall()
        sms_rows = conn.execute(
            "SELECT sms_event_id, device_id, source_package, sender, redacted_message, received_at, "
            "risk_level, indicators_json, explanation, warning, recommendations_json, "
            "detected_languages_json, code_mixed, language_status, engine_version "
            "FROM sms_threat_events WHERE customer_key = ?", (owner,)
        ).fetchall()
    finally:
        conn.close()
    now = datetime.now(timezone.utc).isoformat()
    items = [_assessment_item(row, now) for row in assessment_rows]
    items.extend(_sms_item(row) for row in sms_rows)
    items = [item for item in items if
             (not source_type or item["source_type"] == source_type) and
             (not risk_level or item["risk_level"] == risk_level) and
             (not start or item["analysis_timestamp"] >= start) and
             (not end or item["analysis_timestamp"] <= end)]
    if needle:
        items = [item for item in items if needle in json.dumps(item, ensure_ascii=False).casefold()]
    # Newest timestamp first, with deterministic ascending tie-breakers.
    items.sort(key=lambda x: (x["source_type"], x["report_id"]))
    items.sort(key=lambda x: x["analysis_timestamp"], reverse=True)
    if after:
        t, src, rid = after
        items = [x for x in items if x["analysis_timestamp"] < t or
                 (x["analysis_timestamp"] == t and (x["source_type"], x["report_id"]) > (src, rid))]
    page = items[:limit]
    more = len(items) > limit
    return {"items": page, "next_cursor": _make_cursor(page[-1]) if more and page else None, "has_more": more}


def _date_bound(value: str, *, end: bool) -> str:
    """Accept an ISO date (inclusive whole day) or an ISO datetime."""
    try:
        parsed_date = date.fromisoformat(value)
        point = datetime.combine(parsed_date, time.max if end else time.min, tzinfo=timezone.utc)
    except ValueError:
        point = datetime.fromisoformat(value)
        if point.tzinfo is None:
            point = point.replace(tzinfo=timezone.utc)
        point = point.astimezone(timezone.utc)
    return point.isoformat()


def get_report(principal: CustomerPrincipal, source_type: str, report_id: UUID | str) -> dict:
    source = source_type.upper()
    if source not in SOURCES:
        raise InvalidReportQuery("Unsupported source type.")
    owner = _owner(principal)
    conn = database.get_connection()
    try:
        if source in {"VOICE_CALL", "MANUAL_MESSAGE"}:
            row = conn.execute(
                "SELECT assessment_id, source_mode, risk_level, indicators_json, explanation, warning, "
                "recommended_steps_json, detected_languages_json, is_code_mixed, language_status, engine_version, "
                "created_at, redacted_transcript, transcript_expires_at, transcript_status "
                "FROM scam_assessments WHERE assessment_id = ? AND customer_key = ?",
                (str(report_id), owner),
            ).fetchone()
            if row is None or (source == "VOICE_CALL") != (row["source_mode"] == "voice_call"):
                raise AssessmentNotFoundError
            return _assessment_item(row, datetime.now(timezone.utc).isoformat())
        row = conn.execute(
            "SELECT sms_event_id, device_id, source_package, sender, redacted_message, received_at, risk_level, "
            "indicators_json, explanation, warning, recommendations_json, detected_languages_json, code_mixed, "
            "language_status, engine_version FROM sms_threat_events WHERE sms_event_id = ? AND customer_key = ?",
            (str(report_id), owner),
        ).fetchone()
        if row is None:
            raise SmsThreatNotFoundError("SMS threat event not found.")
        return _sms_item(row)
    finally:
        conn.close()
