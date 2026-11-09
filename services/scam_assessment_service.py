"""Owner-scoped persistence for deterministic scam message assessments."""

from __future__ import annotations

import base64
from datetime import datetime, timezone
import json
import re
import logging
import threading
from datetime import timedelta
from uuid import UUID, uuid4

from services import database
from services.principal import CustomerPrincipal
from services.scam_risk_engine import ENGINE_VERSION, analyze_scam_text


PAGE_MAX = 100
_SUMMARY_COLUMNS = (
    "assessment_id, source_mode AS source_type, risk_level, evidence_strength, "
    "detected_languages_json, language_status, created_at"
)


class InvalidAssessmentError(ValueError):
    """Input or pagination data is invalid."""


class AssessmentNotFoundError(LookupError):
    """Assessment is absent or owned by a different principal."""


class AssessmentEngineError(RuntimeError):
    """The assessment engine failed without exposing its internal details."""


class ScamTranscriptionError(RuntimeError):
    """Safe error boundary for speech transcription failures."""

    def __init__(self, message: str, *, status_code: int = 503, code: str = "SPEECH_PROVIDER_ERROR"):
        super().__init__(message)
        self.status_code = status_code
        self.code = code


def _owner(principal: CustomerPrincipal) -> str:
    if not isinstance(principal, CustomerPrincipal):
        raise TypeError("An authenticated customer principal is required.")
    allowed = frozenset(database.DEMO_CUSTOMER_KEYS.values())
    if principal.customer_key not in allowed:
        raise TypeError("An authenticated customer principal is required.")
    if principal.expires_at <= datetime.now(timezone.utc):
        raise TypeError("An active authenticated customer principal is required.")
    return principal.customer_key


def _redact(text: str, *, max_length: int = 500) -> str:
    """Remove credential values and common direct identifiers from evidence."""
    value = text
    patterns = (
        # Sensitive values near explicit labels, including digit/word codes.
        (r"(?i)\b(otp|one[- ]time password|upi\s*pin|pin|password|passcode|security code|access token|api key|secret)\b\s*(?::|is|=|-)\s*[^\s,.;!?]{1,80}", r"\1 [REDACTED]"),
        # Credential phrases can lack punctuation ("OTP 123456").
        (r"(?i)\b(otp|one[- ]time password|upi\s*pin|pin|password|passcode|security code)\b\s+(\d{3,12}|[A-Z0-9@#$%*!_.-]{12,80})\b", r"\1 [REDACTED]"),
        # Account numbers / long numeric references.
        (r"(?<!\w)(?:\+?\d[\d -]{6,16}\d)(?!\w)", "[NUMBER REDACTED]"),
        # Indian phone-like numbers.
        (r"(?<!\w)(?:\+91[- ]?)?[6-9]\d{9}(?!\w)", "[PHONE REDACTED]"),
        # Sensitive URL query values and bearer/token strings.
        (r"(?i)([?&](?:token|key|auth|code|otp|pin|password)=)[^&\s]+", r"\1[REDACTED]"),
        (r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}", "Bearer [REDACTED]"),
        # Remove email addresses and names are not reliably machine-identifiable;
        # evidence snippets are sentence-level and engine-authored patterns only.
        (r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", "[EMAIL REDACTED]"),
        (r"(?i)\b(hi|hello|dear)\s+[A-Z][a-z]{1,30}\b", r"\1 [NAME REDACTED]"),
        (r"(?i)\bmy name is\s+[A-Z][a-z]{1,30}\b", "my name is [NAME REDACTED]"),
    )
    for pattern, replacement in patterns:
        value = re.sub(pattern, replacement, value)
    return value[:max_length]


def _cursor_offset(cursor: str | None) -> int:
    if not cursor:
        return 0
    try:
        decoded = base64.b64decode(
            cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True
        ).decode("ascii")
        offset = int(decoded)
        if offset < 0 or str(offset) != decoded:
            raise ValueError
        return offset
    except (ValueError, UnicodeError, base64.binascii.Error) as exc:
        raise InvalidAssessmentError("Invalid pagination cursor.") from exc


def _cursor(offset: int) -> str:
    return base64.urlsafe_b64encode(str(offset).encode()).rstrip(b"=").decode()


def _serialize_detail(row) -> dict:
    result = dict(row)
    result["indicators"] = json.loads(result.pop("indicators_json"))
    result["recommended_steps"] = json.loads(result.pop("recommended_steps_json"))
    result["detected_languages"] = json.loads(result.pop("detected_languages_json"))
    result["is_code_mixed"] = bool(result["is_code_mixed"])
    return result


def _public_engine_result(result: dict, *, is_code_mixed: bool = False) -> dict:
    # Engine operates on the unredacted transient input; only its output is
    # sanitized before it is returned or persisted.
    data = dict(result)
    data["indicators"] = [
        {
            **item,
            "evidence": [_redact(str(snippet)) for snippet in item.get("evidence", [])],
            "explanation": _redact(str(item.get("explanation", ""))),
        }
        for item in data.get("indicators", [])
    ]
    for key in ("explanation", "warning"):
        data[key] = _redact(str(data.get(key, "")))
    data["recommended_steps"] = [
        _redact(str(step)) for step in data.get("recommended_steps", [])
    ]
    if is_code_mixed:
        data["is_code_mixed"] = True
    return data


def create_assessment(
    principal: CustomerPrincipal,
    message_text: str,
    language_hint: str = "auto",
    *,
    _source_mode: str = "typed_message",
    _detected_languages: tuple[str, ...] | list[str] | None = None,
    _is_code_mixed: bool = False,
    _redacted_transcript: str | None = None,
) -> dict:
    owner = _owner(principal)
    if not isinstance(message_text, str):
        raise InvalidAssessmentError("Message text must be text.")
    message_text = message_text.strip()
    if not message_text or len(message_text) > 12000:
        raise InvalidAssessmentError("Message text must contain 1 to 12000 characters.")
    if language_hint not in {"auto", "en-IN", "kn-IN", "hi-IN"}:
        raise InvalidAssessmentError("Unsupported language hint.")
    languages = (
        list(_detected_languages)
        if _detected_languages
        else (None if language_hint == "auto" else [language_hint])
    )
    if _source_mode not in {"typed_message", "voice_call"}:
        raise InvalidAssessmentError("Unsupported assessment source.")
    if _redacted_transcript is not None and _source_mode != "voice_call":
        raise InvalidAssessmentError("Transcripts are only valid for call assessments.")
    try:
        result = _public_engine_result(
            analyze_scam_text(message_text, detected_languages=languages).to_dict(),
            is_code_mixed=_is_code_mixed,
        )
    except Exception as exc:
        raise AssessmentEngineError from exc

    assessment_id = str(uuid4())
    created_at = datetime.now(timezone.utc).isoformat()
    transcript_expires_at = (
        (datetime.fromisoformat(created_at) + timedelta(days=30)).isoformat()
        if _redacted_transcript is not None else None
    )
    row = {
        "assessment_id": assessment_id,
        "source_type": "typed_message",
        **result,
        "created_at": created_at,
    }
    # Bind the server-side identity and all values; original message is not
    # included in schema or SQL.
    conn = database.get_connection()
    try:
        with conn:
            conn.execute(
                """INSERT INTO scam_assessments (
                    assessment_id, customer_key, source_type, source_mode, risk_level,
                    indicators_json, explanation, evidence_strength, warning,
                    recommended_steps_json, detected_languages_json,
                    is_code_mixed, language_status, engine_version, created_at,
                    redacted_transcript, transcript_expires_at, transcript_status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    assessment_id, owner, "typed_message", _source_mode, row["risk_level"],
                    json.dumps(row["indicators"], ensure_ascii=False),
                    row["explanation"], row["evidence_strength"], row["warning"],
                    json.dumps(row["recommended_steps"], ensure_ascii=False),
                    json.dumps(row["detected_languages"]), int(row["is_code_mixed"]),
                    row["language_status"], ENGINE_VERSION, created_at,
                    _redacted_transcript, transcript_expires_at,
                    "AVAILABLE" if _redacted_transcript is not None else "NOT_STORED",
                ),
            )
    finally:
        conn.close()
    return get_assessment(principal, assessment_id)


def create_voice_assessment(
    principal: CustomerPrincipal,
    audio_upload_id: UUID,
    language_hint: str = "auto",
    *,
    transcriber=None,
) -> dict:
    """Consume one owned upload and retain only a redacted transcript for 30 days."""
    _owner(principal)
    if language_hint not in {"auto", "en-IN", "kn-IN", "hi-IN"}:
        raise InvalidAssessmentError("Unsupported language hint.")
    try:
        if transcriber is None:
            from services.speech_adapters import create_speech_transcriber
            transcriber = create_speech_transcriber()
        transcription = transcriber.transcribe(principal, audio_upload_id, language_hint)
    except Exception as exc:
        code = getattr(exc, "code", "SPEECH_PROVIDER_ERROR")
        message = "Speech transcription failed. The recording was not assessed."
        status_code = 503
        if code == "UNSUPPORTED_AUDIO_FORMAT":
            message, status_code = "This audio format is not supported by the configured speech provider.", 415
        elif code == "AUDIO_UPLOAD_UNAVAILABLE":
            message, status_code = "Audio upload is unavailable, expired, or inaccessible.", 404
        elif code == "EMPTY_TRANSCRIPT":
            message, status_code = "No transcript was produced, so no scam assessment was created.", 422
        raise ScamTranscriptionError(message, status_code=status_code, code=code) from None

    transcript = getattr(transcription, "text", None)
    if not isinstance(transcript, str) or not transcript.strip():
        raise ScamTranscriptionError(
            "No transcript was produced, so no scam assessment was created.",
            status_code=422,
            code="EMPTY_TRANSCRIPT",
        )
    reported_languages = getattr(transcription, "languages", ())
    if not isinstance(reported_languages, (tuple, list)):
        reported_languages = ()
    supported = {"kn-IN", "en-IN", "hi-IN"}
    languages = tuple(dict.fromkeys(
        code for code in reported_languages
        if isinstance(code, str) and code in supported
    ))
    assessment = create_assessment(
        principal,
        transcript,
        "auto",
        _source_mode="voice_call",
        _detected_languages=languages,
        _is_code_mixed=bool(getattr(transcription, "is_code_mixed", False)),
        _redacted_transcript=_redact(transcript.strip(), max_length=12000),
    )
    assessment["transcript"] = assessment.pop("redacted_transcript", None)
    assessment["transcript_expires_at"] = assessment.get("transcript_expires_at")
    assessment["transcription_status"] = "completed"
    assessment["transcription_warning"] = (
        "Automated transcription may contain errors. Review the transcript before relying on this assessment."
    )
    return assessment


def list_assessments(
    principal: CustomerPrincipal, limit: int = 25, cursor: str | None = None
) -> dict:
    owner = _owner(principal)
    if not 1 <= limit <= PAGE_MAX:
        raise InvalidAssessmentError("limit must be between 1 and 100.")
    offset = _cursor_offset(cursor)
    conn = database.get_connection()
    try:
        rows = conn.execute(
            f"SELECT {_SUMMARY_COLUMNS} FROM scam_assessments "
            "WHERE customer_key = ? ORDER BY created_at DESC, assessment_id DESC LIMIT ? OFFSET ?",
            (owner, limit + 1, offset),
        ).fetchall()
    finally:
        conn.close()
    has_more = len(rows) > limit
    items = []
    for raw in rows[:limit]:
        item = dict(raw)
        item["detected_languages"] = json.loads(item.pop("detected_languages_json"))
        items.append(item)
    return {
        "items": items,
        "next_cursor": _cursor(offset + limit) if has_more else None,
        "has_more": has_more,
    }


def get_assessment(
    principal: CustomerPrincipal, assessment_id: UUID | str
) -> dict:
    owner = _owner(principal)
    conn = database.get_connection()
    try:
        row = conn.execute(
            "SELECT assessment_id, source_mode AS source_type, risk_level, indicators_json, "
            "explanation, evidence_strength, warning, recommended_steps_json, "
            "detected_languages_json, is_code_mixed, language_status, engine_version, created_at "
            ", redacted_transcript, transcript_expires_at, transcript_status "
            "FROM scam_assessments WHERE assessment_id = ? AND customer_key = ?",
            (str(assessment_id), owner),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        raise AssessmentNotFoundError
    result = _serialize_detail(row)
    if (result.get("transcript_status") == "AVAILABLE"
            and result.get("transcript_expires_at")
            and result["transcript_expires_at"] <= datetime.now(timezone.utc).isoformat()):
        result["redacted_transcript"] = None
        result["transcript_status"] = "EXPIRED"
    return result


_cleanup_stop = threading.Event()
_cleanup_thread: threading.Thread | None = None


def cleanup_expired_transcripts() -> int:
    """Scrub expired transcript fields atomically while preserving report rows."""
    now = datetime.now(timezone.utc).isoformat()
    conn = database.get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        cur = conn.execute(
            "UPDATE scam_assessments SET redacted_transcript = NULL, "
            "transcript_expires_at = NULL, transcript_status = 'EXPIRED' "
            "WHERE transcript_status = 'AVAILABLE' AND transcript_expires_at <= ?",
            (now,),
        )
        conn.commit()
        return cur.rowcount
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _transcript_cleanup_loop() -> None:
    logger = logging.getLogger(__name__)
    while not _cleanup_stop.wait(60):
        try:
            cleanup_expired_transcripts()
        except Exception as exc:
            logger.warning("transcript_cleanup_failed error=%s", type(exc).__name__)


def start_transcript_cleanup_worker() -> None:
    global _cleanup_thread
    if _cleanup_thread is not None and _cleanup_thread.is_alive():
        return
    _cleanup_stop.clear()
    _cleanup_thread = threading.Thread(
        target=_transcript_cleanup_loop, name="transcript-cleanup", daemon=True
    )
    _cleanup_thread.start()


def stop_transcript_cleanup_worker() -> None:
    global _cleanup_thread
    _cleanup_stop.set()
    thread = _cleanup_thread
    if thread is not None:
        thread.join(timeout=2)
    _cleanup_thread = None
