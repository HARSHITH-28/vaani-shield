"""Customer-isolated Android companion pairing and SMS threat intelligence service.

Implements Phase 2 backend support for VaaniShield 2.0:
- Cryptographically random 8-character single-use pairing codes (SHA-256 hashed).
- Narrowly scoped 256-bit device bearer tokens (SHA-256 hashed) bound to DevicePrincipal.
- Deterministic multilingual scam analysis reusing services.scam_risk_engine.
- Strict PII/credential redaction before SQLite persistence.
- Customer- and device-scoped idempotent deduplication.
- High/Critical security alert integration with services.notification_service.
"""

from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
import re
import secrets
import sqlite3
import threading
from uuid import UUID, uuid4

from services import database
from services.notification_service import create_sms_threat_notification
from services.principal import CustomerPrincipal, DevicePrincipal
from services.scam_assessment_service import _public_engine_result, _redact
from services.scam_risk_engine import ENGINE_VERSION, analyze_scam_text


# Exclude visually ambiguous characters: O, 0, I, 1
PAIRING_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
PAIRING_CODE_LENGTH = 8
PAIRING_CODE_TTL_SECONDS = 300  # 5 minutes

ALLOWED_SOURCE_PACKAGES = frozenset({"com.google.android.apps.messaging"})
ALLOWED_SENDER_TYPES = frozenset(
    {
        "dlt_header",
        "phone_number",
        "phone",
        "alphanumeric",
        "short_code",
        "shortcode",
        "contact",
        "unknown",
    }
)
ALLOWED_RISK_LEVELS = frozenset({"UNKNOWN", "LOW", "MEDIUM", "HIGH", "CRITICAL"})
MAX_SMS_MESSAGE_LENGTH = 2000
PAGE_MAX = 100

# Brute-force & abuse rate limiting (scoped by DB_PATH for clean test isolation)
PAIR_TOKEN_GENERATE_MAX = 10
PAIR_TOKEN_GENERATE_WINDOW_SECONDS = 300
PAIR_ATTEMPT_MAX = 5
PAIR_ATTEMPT_WINDOW_SECONDS = 300

_rate_limit_lock = threading.Lock()
_rate_limit_buckets: dict[tuple[str, str, str], list[datetime]] = {}


class SmsThreatServiceError(Exception):
    """Base error for SMS threat intelligence operations."""


class InvalidSmsInputError(SmsThreatServiceError, ValueError):
    """Request input, filter, or pagination cursor failed validation."""


class PairingCodeInvalidError(SmsThreatServiceError):
    """Pairing code does not match any record."""


class PairingCodeExpiredError(SmsThreatServiceError):
    """Pairing code has expired."""


class PairingCodeReusedError(SmsThreatServiceError):
    """Pairing code was already consumed or invalidated."""


class DeviceConflictError(SmsThreatServiceError):
    """Device identifier conflicts with an existing registration."""


class DeviceAuthenticationError(SmsThreatServiceError):
    """Device bearer token is missing or invalid."""


class DeviceAccessForbiddenError(SmsThreatServiceError):
    """Device is paused or revoked and may not perform this operation."""


class DeviceNotFoundError(SmsThreatServiceError, LookupError):
    """Device does not exist or belongs to another customer."""


class DeviceStateConflictError(SmsThreatServiceError):
    """Requested device status transition is not permitted."""


class SmsThreatNotFoundError(SmsThreatServiceError, LookupError):
    """SMS threat event does not exist or belongs to another customer."""


class RateLimitExceededError(SmsThreatServiceError):
    """Too many pairing code generation or verification attempts."""


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _get_db_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(database.DB_PATH, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _owner(principal: CustomerPrincipal) -> str:
    if not isinstance(principal, CustomerPrincipal):
        raise TypeError("An authenticated customer principal is required.")
    allowed = frozenset(database.DEMO_CUSTOMER_KEYS.values())
    if principal.customer_key not in allowed:
        raise TypeError("An authenticated customer principal is required.")
    if principal.expires_at <= _now_utc():
        raise TypeError("An active authenticated customer principal is required.")
    return principal.customer_key


def _require_active_device(principal: DevicePrincipal) -> DevicePrincipal:
    if not isinstance(principal, DevicePrincipal):
        raise DeviceAuthenticationError("A valid device principal is required.")
    if principal.auth_method != "device_bearer" or principal.status != "ACTIVE":
        raise DeviceAccessForbiddenError("Only an active paired device is permitted.")
    return principal


def reset_rate_limits() -> None:
    """Clear in-memory rate-limit counters (used for isolated testing)."""
    with _rate_limit_lock:
        _rate_limit_buckets.clear()


def _check_rate_limit(
    action: str,
    subject_key: str,
    *,
    max_attempts: int,
    window_seconds: int,
    record_immediately: bool = False,
) -> None:
    now = _now_utc()
    cutoff = now - timedelta(seconds=window_seconds)
    bucket_key = (str(database.DB_PATH), action, subject_key)
    with _rate_limit_lock:
        history = [ts for ts in _rate_limit_buckets.get(bucket_key, []) if ts > cutoff]
        if len(history) >= max_attempts:
            _rate_limit_buckets[bucket_key] = history
            raise RateLimitExceededError("Too many attempts. Please try again later.")
        if record_immediately:
            history.append(now)
        _rate_limit_buckets[bucket_key] = history


def _record_rate_limit_event(action: str, subject_key: str, *, window_seconds: int) -> None:
    now = _now_utc()
    cutoff = now - timedelta(seconds=window_seconds)
    bucket_key = (str(database.DB_PATH), action, subject_key)
    with _rate_limit_lock:
        history = [ts for ts in _rate_limit_buckets.get(bucket_key, []) if ts > cutoff]
        history.append(now)
        _rate_limit_buckets[bucket_key] = history


def _generate_pairing_code() -> str:
    return "".join(secrets.choice(PAIRING_CODE_ALPHABET) for _ in range(PAIRING_CODE_LENGTH))


def _generate_device_token() -> str:
    # 32 random bytes = 256 bits of cryptographic entropy.
    return f"vsd_{secrets.token_hex(32)}"


def redact_sms_text(text: str) -> str:
    """Redact credentials, OTPs, PINs, account numbers, phones, and tokens from SMS text.

    Reuses services.scam_assessment_service._redact and adds SMS-specific patterns
    for non-adjacent or reversed OTP/MPIN/CVV phrasing.
    """
    value = str(text or "")
    sms_patterns = (
        # OTP / PIN / MPIN / CVV / verification code followed by words then 4-8 digits
        # e.g., "Your OTP for transaction is 482910" -> "Your OTP for transaction is [REDACTED]"
        (
            r"(?i)\b(otp|one[- ]time password|upi\s*pin|mpin|atm\s*pin|pin|cvv|verification code|security code|passcode|login code|auth code)\b"
            r"([^.!?\n]{0,40}?(?:\b(?:is|code)\b|[:=-])\s*)\b\d{3,10}\b",
            r"\1\2[REDACTED]",
        ),
        # Explicit MPIN / CVV / verification code adjacent to value
        (
            r"(?i)\b(mpin|cvv|verification code|login code|auth code)\b\s*(?::|is|=|-)?\s*\b(\d{3,10}|[A-Z0-9@#$%*!_.-]{6,40})\b",
            r"\1 [REDACTED]",
        ),
        # Reversed phrasing: "482910 is your OTP" or "482910 is the verification code"
        (
            r"(?i)\b\d{4,8}\b(?=\s+(?:is\s+)?(?:your\s+|the\s+)?(?:bank\s+|login\s+|transaction\s+)?(?:otp|one[- ]time password|upi\s*pin|mpin|pin|verification code|security code|passcode)\b)",
            "[REDACTED]",
        ),
    )
    for pattern, replacement in sms_patterns:
        value = re.sub(pattern, replacement, value)
    return _redact(value)


def _sanitize_sender(sender: str) -> str:
    """Ensure sender metadata cannot leak credentials while preserving sender identity."""
    cleaned = sender.strip()
    # Redact any accidental credential/token in sender while preserving normal headers/numbers
    cleaned = re.sub(
        r"(?i)\b(otp|pin|mpin|password|passcode|token|secret)\b\s*[:=-]?\s*\S+",
        r"\1 [REDACTED]",
        cleaned,
    )
    cleaned = re.sub(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}", "Bearer [REDACTED]", cleaned)
    return cleaned[:120]


def _parse_iso_timestamp(raw_ts: str) -> datetime:
    text = str(raw_ts or "").strip()
    if not text:
        raise InvalidSmsInputError("message_timestamp is required.")
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise InvalidSmsInputError("message_timestamp must be a valid ISO-8601 timestamp.") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    else:
        parsed = parsed.astimezone(timezone.utc)

    now = _now_utc()
    if parsed > now + timedelta(minutes=10):
        raise InvalidSmsInputError("message_timestamp cannot be in the future.")
    if parsed < now - timedelta(days=365):
        raise InvalidSmsInputError("message_timestamp is too old to be ingested.")
    return parsed


def _serialize_device(row: sqlite3.Row | dict) -> dict:
    data = dict(row)
    return {
        "device_id": data["device_id"],
        "device_name": data["device_name"],
        "device_model": data.get("device_model"),
        "android_version": data.get("android_version"),
        "app_version": data.get("app_version"),
        "status": data["status"],
        "created_at": data["created_at"],
        "paired_at": data.get("paired_at"),
        "last_seen_at": data.get("last_seen_at"),
        "revoked_at": data.get("revoked_at"),
        "updated_at": data["updated_at"],
    }


def _serialize_sms_summary(row: sqlite3.Row | dict) -> dict:
    data = dict(row)
    return {
        "sms_event_id": data["sms_event_id"],
        "device_id": data["device_id"],
        "source_package": data["source_package"],
        "sender": data["sender"],
        "sender_type": data["sender_type"],
        "redacted_message": data["redacted_message"],
        "message_timestamp": data["message_timestamp"],
        "received_at": data["received_at"],
        "risk_level": data["risk_level"],
        "evidence_strength": data["evidence_strength"],
        "detected_languages": json.loads(data["detected_languages_json"]),
        "code_mixed": bool(data["code_mixed"]),
        "language_status": data["language_status"],
        "alert_generated": bool(data["alert_generated"]),
        "acknowledged": bool(data["acknowledged"]),
        "acknowledged_at": data.get("acknowledged_at"),
    }


def _serialize_sms_detail(row: sqlite3.Row | dict) -> dict:
    data = dict(row)
    summary = _serialize_sms_summary(data)
    summary.update(
        {
            "indicators": json.loads(data["indicators_json"]),
            "explanation": data["explanation"],
            "warning": data["warning"],
            "recommendations": json.loads(data["recommendations_json"]),
            "engine_version": data["engine_version"],
        }
    )
    return summary


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
        raise InvalidSmsInputError("Invalid pagination cursor.") from exc


def _cursor(offset: int) -> str:
    return base64.urlsafe_b64encode(str(offset).encode()).rstrip(b"=").decode()


def create_pairing_code(
    principal: CustomerPrincipal,
    *,
    device_name: str | None = None,
) -> dict:
    """Generate a single-use 8-character pairing code valid for 5 minutes."""
    owner = _owner(principal)
    _check_rate_limit(
        "pair_token_generate",
        owner,
        max_attempts=PAIR_TOKEN_GENERATE_MAX,
        window_seconds=PAIR_TOKEN_GENERATE_WINDOW_SECONDS,
        record_immediately=True,
    )

    now = _now_utc()
    now_iso = now.isoformat()
    expires_at = now + timedelta(seconds=PAIRING_CODE_TTL_SECONDS)
    expires_iso = expires_at.isoformat()
    label = (device_name or "Pending Android Companion").strip()[:120]

    conn = _get_db_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        # Invalidate any previously expired PENDING records for this customer.
        conn.execute(
            """
            UPDATE paired_devices
            SET status = 'REVOKED',
                revoked_at = COALESCE(revoked_at, ?),
                updated_at = ?
            WHERE customer_key = ?
              AND status = 'PENDING'
              AND pairing_code_expires_at <= ?
            """,
            (now_iso, now_iso, owner, now_iso),
        )

        # Generate a unique 8-character code and store only its SHA-256 hash.
        while True:
            raw_code = _generate_pairing_code()
            code_hash = _sha256_hex(raw_code)
            collision = conn.execute(
                "SELECT 1 FROM paired_devices WHERE pairing_code_hash = ? LIMIT 1",
                (code_hash,),
            ).fetchone()
            if not collision:
                break

        placeholder_device_id = f"pending-{uuid4().hex}"
        conn.execute(
            """
            INSERT INTO paired_devices (
                device_id, customer_key, device_name, status,
                pairing_code_hash, pairing_code_expires_at,
                created_at, updated_at
            ) VALUES (?, ?, ?, 'PENDING', ?, ?, ?, ?)
            """,
            (
                placeholder_device_id,
                owner,
                label,
                code_hash,
                expires_iso,
                now_iso,
                now_iso,
            ),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    return {
        "pairing_code": raw_code,
        "expires_at": expires_iso,
        "expires_in_seconds": PAIRING_CODE_TTL_SECONDS,
    }


def pair_device(
    *,
    pairing_code: str,
    device_id: str,
    device_name: str,
    device_model: str,
    android_version: str,
    app_version: str,
    client_key: str = "global",
) -> dict:
    """Atomically verify and consume a pairing code, returning a 256-bit device token once."""
    _check_rate_limit(
        "pair_exchange_failed",
        client_key,
        max_attempts=PAIR_ATTEMPT_MAX,
        window_seconds=PAIR_ATTEMPT_WINDOW_SECONDS,
        record_immediately=False,
    )

    normalized_code = str(pairing_code or "").strip().upper()
    if (
        len(normalized_code) != PAIRING_CODE_LENGTH
        or any(ch not in PAIRING_CODE_ALPHABET for ch in normalized_code)
    ):
        _record_rate_limit_event(
            "pair_exchange_failed", client_key, window_seconds=PAIR_ATTEMPT_WINDOW_SECONDS
        )
        raise PairingCodeInvalidError("Invalid pairing code.")

    clean_device_id = str(device_id or "").strip()
    if not clean_device_id or clean_device_id.startswith("pending-"):
        raise InvalidSmsInputError("Invalid device_id.")

    code_hash = _sha256_hex(normalized_code)
    now = _now_utc()
    now_iso = now.isoformat()

    conn = _get_db_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            """
            SELECT id, customer_key, status, pairing_code_hash, pairing_code_expires_at, paired_at
            FROM paired_devices
            WHERE pairing_code_hash = ?
            """,
            (code_hash,),
        ).fetchone()

        if row is None or not hmac.compare_digest(row["pairing_code_hash"], code_hash):
            conn.rollback()
            _record_rate_limit_event(
                "pair_exchange_failed", client_key, window_seconds=PAIR_ATTEMPT_WINDOW_SECONDS
            )
            raise PairingCodeInvalidError("Invalid pairing code.")

        if row["paired_at"] is not None or row["status"] != "PENDING":
            conn.rollback()
            _record_rate_limit_event(
                "pair_exchange_failed", client_key, window_seconds=PAIR_ATTEMPT_WINDOW_SECONDS
            )
            raise PairingCodeReusedError("Pairing code has already been used.")

        expires_raw = row["pairing_code_expires_at"]
        try:
            expires_dt = datetime.fromisoformat(expires_raw)
            if expires_dt.tzinfo is None:
                expires_dt = expires_dt.replace(tzinfo=timezone.utc)
        except Exception:
            expires_dt = now - timedelta(seconds=1)

        if expires_dt <= now:
            conn.execute(
                """
                UPDATE paired_devices
                SET status = 'REVOKED',
                    revoked_at = COALESCE(revoked_at, ?),
                    updated_at = ?
                WHERE id = ?
                """,
                (now_iso, now_iso, row["id"]),
            )
            conn.commit()
            _record_rate_limit_event(
                "pair_exchange_failed", client_key, window_seconds=PAIR_ATTEMPT_WINDOW_SECONDS
            )
            raise PairingCodeExpiredError("Pairing code has expired.")

        existing_device = conn.execute(
            "SELECT id, customer_key, status FROM paired_devices WHERE device_id = ? AND id <> ?",
            (clean_device_id, row["id"]),
        ).fetchone()
        if existing_device is not None:
            conn.rollback()
            raise DeviceConflictError("Device ID is already registered.")

        while True:
            raw_token = _generate_device_token()
            token_hash = _sha256_hex(raw_token)
            token_collision = conn.execute(
                "SELECT 1 FROM paired_devices WHERE token_hash = ? LIMIT 1",
                (token_hash,),
            ).fetchone()
            if not token_collision:
                break

        cur = conn.execute(
            """
            UPDATE paired_devices
            SET device_id = ?,
                device_name = ?,
                device_model = ?,
                android_version = ?,
                app_version = ?,
                status = 'ACTIVE',
                token_hash = ?,
                paired_at = ?,
                last_seen_at = ?,
                updated_at = ?
            WHERE id = ?
              AND status = 'PENDING'
              AND paired_at IS NULL
            """,
            (
                clean_device_id,
                device_name.strip()[:120],
                device_model.strip()[:120],
                android_version.strip()[:32],
                app_version.strip()[:32],
                token_hash,
                now_iso,
                now_iso,
                now_iso,
                row["id"],
            ),
        )
        if cur.rowcount != 1:
            conn.rollback()
            raise PairingCodeReusedError("Pairing code has already been used.")

        conn.commit()
    except SmsThreatServiceError:
        raise
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    return {
        "device_id": clean_device_id,
        "device_name": device_name.strip()[:120],
        "device_model": device_model.strip()[:120],
        "android_version": android_version.strip()[:32],
        "app_version": app_version.strip()[:32],
        "status": "ACTIVE",
        "device_token": raw_token,
        "paired_at": now_iso,
    }


def authenticate_device_token(raw_token: str | None) -> DevicePrincipal:
    """Validate an Android companion Bearer token and return a narrowly scoped DevicePrincipal."""
    if not raw_token or not isinstance(raw_token, str):
        raise DeviceAuthenticationError("Missing device authentication token.")
    cleaned = raw_token.strip()
    if not cleaned.startswith("vsd_") or len(cleaned) < 36 or len(cleaned) > 128:
        raise DeviceAuthenticationError("Invalid device authentication token.")

    candidate_hash = _sha256_hex(cleaned)
    conn = _get_db_connection()
    try:
        row = conn.execute(
            """
            SELECT device_id, customer_key, device_name, status, token_hash
            FROM paired_devices
            WHERE token_hash = ?
            """,
            (candidate_hash,),
        ).fetchone()
    finally:
        conn.close()

    if row is None or not row["token_hash"] or not hmac.compare_digest(row["token_hash"], candidate_hash):
        raise DeviceAuthenticationError("Invalid device authentication token.")

    status = row["status"]
    if status == "REVOKED":
        raise DeviceAccessForbiddenError("Device access has been revoked.")
    if status == "PAUSED":
        raise DeviceAccessForbiddenError("Device is currently paused.")
    if status != "ACTIVE":
        raise DeviceAuthenticationError("Device is not active.")

    return DevicePrincipal(
        device_id=row["device_id"],
        customer_key=row["customer_key"],
        device_name=row["device_name"],
        status="ACTIVE",
        auth_method="device_bearer",
    )


def record_device_heartbeat(
    device_principal: DevicePrincipal,
    *,
    app_version: str | None = None,
) -> dict:
    """Update last_seen_at for an active paired device."""
    principal = _require_active_device(device_principal)
    now_iso = _now_utc().isoformat()

    conn = _get_db_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            """
            SELECT id, status
            FROM paired_devices
            WHERE device_id = ? AND customer_key = ?
            """,
            (principal.device_id, principal.customer_key),
        ).fetchone()
        if row is None:
            conn.rollback()
            raise DeviceAuthenticationError("Device not found.")
        if row["status"] != "ACTIVE":
            conn.rollback()
            raise DeviceAccessForbiddenError("Device is not active.")

        if app_version:
            conn.execute(
                """
                UPDATE paired_devices
                SET last_seen_at = ?, app_version = ?, updated_at = ?
                WHERE id = ?
                """,
                (now_iso, app_version.strip()[:32], now_iso, row["id"]),
            )
        else:
            conn.execute(
                """
                UPDATE paired_devices
                SET last_seen_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (now_iso, now_iso, row["id"]),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    return {
        "device_id": principal.device_id,
        "status": "ACTIVE",
        "forwarding_permitted": True,
        "last_seen_at": now_iso,
        "server_time": now_iso,
    }


def list_customer_devices(principal: CustomerPrincipal) -> dict:
    """List paired companion devices belonging to the authenticated customer."""
    owner = _owner(principal)
    conn = _get_db_connection()
    try:
        rows = conn.execute(
            """
            SELECT device_id, device_name, device_model, android_version, app_version,
                   status, created_at, paired_at, last_seen_at, revoked_at, updated_at
            FROM paired_devices
            WHERE customer_key = ?
              AND status IN ('ACTIVE', 'PAUSED', 'REVOKED')
            ORDER BY paired_at DESC, id DESC
            """,
            (owner,),
        ).fetchall()
    finally:
        conn.close()

    return {"items": [_serialize_device(row) for row in rows]}


def update_customer_device_status(
    principal: CustomerPrincipal,
    device_id: str,
    requested_status: str,
) -> dict:
    """Pause, resume, or permanently revoke a customer-owned paired device."""
    owner = _owner(principal)
    clean_device_id = str(device_id or "").strip()
    if not clean_device_id:
        raise DeviceNotFoundError("Device not found.")

    status_map = {
        "PAUSE": "PAUSED",
        "PAUSED": "PAUSED",
        "RESUME": "ACTIVE",
        "ACTIVE": "ACTIVE",
        "REVOKE": "REVOKED",
        "REVOKED": "REVOKED",
    }
    target_status = status_map.get(str(requested_status or "").strip().upper())
    if target_status is None:
        raise InvalidSmsInputError("Unsupported device status transition.")

    now_iso = _now_utc().isoformat()
    conn = _get_db_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            """
            SELECT id, device_id, customer_key, status
            FROM paired_devices
            WHERE device_id = ? AND customer_key = ? AND status != 'PENDING'
            """,
            (clean_device_id, owner),
        ).fetchone()
        if row is None:
            conn.rollback()
            raise DeviceNotFoundError("Device not found.")

        current_status = row["status"]
        if current_status == "REVOKED" and target_status != "REVOKED":
            conn.rollback()
            raise DeviceStateConflictError("A revoked device cannot be reactivated.")

        if target_status == "REVOKED":
            conn.execute(
                """
                UPDATE paired_devices
                SET status = 'REVOKED',
                    revoked_at = COALESCE(revoked_at, ?),
                    updated_at = ?
                WHERE id = ?
                """,
                (now_iso, now_iso, row["id"]),
            )
        else:
            conn.execute(
                """
                UPDATE paired_devices
                SET status = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (target_status, now_iso, row["id"]),
            )

        updated = conn.execute(
            """
            SELECT device_id, device_name, device_model, android_version, app_version,
                   status, created_at, paired_at, last_seen_at, revoked_at, updated_at
            FROM paired_devices
            WHERE id = ?
            """,
            (row["id"],),
        ).fetchone()
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    return _serialize_device(updated)


def rotate_customer_device_token(
    principal: CustomerPrincipal,
    device_id: str,
) -> dict:
    """Rotate the bearer token for an active or paused customer-owned device."""
    owner = _owner(principal)
    clean_device_id = str(device_id or "").strip()
    if not clean_device_id:
        raise DeviceNotFoundError("Device not found.")

    now_iso = _now_utc().isoformat()
    conn = _get_db_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            """
            SELECT id, device_id, status
            FROM paired_devices
            WHERE device_id = ? AND customer_key = ? AND status != 'PENDING'
            """,
            (clean_device_id, owner),
        ).fetchone()
        if row is None:
            conn.rollback()
            raise DeviceNotFoundError("Device not found.")
        if row["status"] == "REVOKED":
            conn.rollback()
            raise DeviceStateConflictError("Cannot rotate token for a revoked device.")

        while True:
            new_token = _generate_device_token()
            new_hash = _sha256_hex(new_token)
            collision = conn.execute(
                "SELECT 1 FROM paired_devices WHERE token_hash = ? LIMIT 1",
                (new_hash,),
            ).fetchone()
            if not collision:
                break

        conn.execute(
            """
            UPDATE paired_devices
            SET token_hash = ?, updated_at = ?
            WHERE id = ?
            """,
            (new_hash, now_iso, row["id"]),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    return {
        "device_id": row["device_id"],
        "status": row["status"],
        "device_token": new_token,
        "updated_at": now_iso,
    }


def ingest_sms_event(
    device_principal: DevicePrincipal,
    *,
    source_package: str,
    sender: str,
    sender_type: str,
    message_text: str,
    message_timestamp: str,
    client_dedup_key: str,
) -> dict:
    """Analyze an incoming Android SMS notification and persist its redacted threat event."""
    principal = _require_active_device(device_principal)

    pkg = str(source_package or "").strip()
    if pkg not in ALLOWED_SOURCE_PACKAGES:
        raise InvalidSmsInputError(
            "Unsupported source_package. Only com.google.android.apps.messaging is supported."
        )

    clean_sender = _sanitize_sender(str(sender or ""))
    if not clean_sender:
        raise InvalidSmsInputError("sender is required.")

    clean_sender_type = str(sender_type or "").strip().lower()
    if clean_sender_type not in ALLOWED_SENDER_TYPES:
        raise InvalidSmsInputError("Unsupported sender_type.")

    if not isinstance(message_text, str):
        raise InvalidSmsInputError("message_text must be a string.")
    clean_message = message_text.strip()
    if not clean_message or len(clean_message) > MAX_SMS_MESSAGE_LENGTH:
        raise InvalidSmsInputError(
            f"message_text must contain 1 to {MAX_SMS_MESSAGE_LENGTH} characters."
        )

    parsed_ts = _parse_iso_timestamp(message_timestamp)
    normalized_ts_iso = parsed_ts.isoformat()

    clean_dedup_key = str(client_dedup_key or "").strip()
    if not clean_dedup_key or len(clean_dedup_key) > 256 or any(ord(c) < 32 for c in clean_dedup_key):
        raise InvalidSmsInputError("client_dedup_key must be 1 to 256 printable characters.")

    dedup_fingerprint = _sha256_hex(
        f"{principal.customer_key}|{principal.device_id}|{clean_dedup_key}"
    )

    # Execute the existing deterministic multilingual scam engine on transient text,
    # then redact all evidence snippets, explanations, warnings, and the stored preview.
    engine_raw = analyze_scam_text(clean_message).to_dict()
    detected_languages = list(engine_raw.get("detected_languages") or [])
    is_code_mixed = len(detected_languages) > 1 or bool(engine_raw.get("is_code_mixed"))
    sanitized_engine = _public_engine_result(engine_raw, is_code_mixed=is_code_mixed)

    indicators = [
        {
            **item,
            "evidence": [redact_sms_text(str(snippet)) for snippet in item.get("evidence", [])],
            "explanation": redact_sms_text(str(item.get("explanation", ""))),
        }
        for item in sanitized_engine.get("indicators", [])
    ]
    explanation = redact_sms_text(str(sanitized_engine.get("explanation", "")))
    warning = redact_sms_text(str(sanitized_engine.get("warning", "")))
    recommendations = [
        redact_sms_text(str(step)) for step in sanitized_engine.get("recommended_steps", [])
    ]
    redacted_message = redact_sms_text(clean_message)
    risk_level = str(sanitized_engine["risk_level"])
    evidence_strength = str(sanitized_engine["evidence_strength"])
    language_status = str(sanitized_engine["language_status"])

    now_iso = _now_utc().isoformat()
    sms_event_id = str(uuid4())
    should_alert = risk_level in {"HIGH", "CRITICAL"}

    conn = _get_db_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        # Confirm device is still ACTIVE in the database inside the write transaction.
        dev_row = conn.execute(
            """
            SELECT id, status
            FROM paired_devices
            WHERE device_id = ? AND customer_key = ?
            """,
            (principal.device_id, principal.customer_key),
        ).fetchone()
        if dev_row is None:
            conn.rollback()
            raise DeviceAuthenticationError("Device not found.")
        if dev_row["status"] != "ACTIVE":
            conn.rollback()
            raise DeviceAccessForbiddenError("Device is not active and cannot submit SMS events.")

        existing = conn.execute(
            """
            SELECT sms_event_id, device_id, risk_level, evidence_strength,
                   alert_generated, received_at
            FROM sms_threat_events
            WHERE customer_key = ? AND dedup_fingerprint = ?
            """,
            (principal.customer_key, dedup_fingerprint),
        ).fetchone()

        if existing is not None:
            conn.execute(
                "UPDATE paired_devices SET last_seen_at = ?, updated_at = ? WHERE id = ?",
                (now_iso, now_iso, dev_row["id"]),
            )
            conn.commit()
            return {
                "sms_event_id": existing["sms_event_id"],
                "device_id": existing["device_id"],
                "deduplicated": True,
                "risk_level": existing["risk_level"],
                "evidence_strength": existing["evidence_strength"],
                "alert_generated": bool(existing["alert_generated"]),
                "received_at": existing["received_at"],
            }

        conn.execute(
            """
            INSERT INTO sms_threat_events (
                sms_event_id, customer_key, device_id, dedup_fingerprint,
                source_package, sender, sender_type, redacted_message,
                message_timestamp, received_at, risk_level, evidence_strength,
                indicators_json, explanation, warning, recommendations_json,
                detected_languages_json, code_mixed, language_status,
                engine_version, alert_generated, acknowledged, acknowledged_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, NULL)
            """,
            (
                sms_event_id,
                principal.customer_key,
                principal.device_id,
                dedup_fingerprint,
                pkg,
                clean_sender,
                clean_sender_type,
                redacted_message,
                normalized_ts_iso,
                now_iso,
                risk_level,
                evidence_strength,
                json.dumps(indicators, ensure_ascii=False),
                explanation,
                warning,
                json.dumps(recommendations, ensure_ascii=False),
                json.dumps(detected_languages),
                int(bool(is_code_mixed)),
                language_status,
                ENGINE_VERSION,
                int(should_alert),
            ),
        )

        if should_alert:
            create_sms_threat_notification(
                principal.customer_key,
                sms_event_id,
                risk_level,
                clean_sender,
                warning,
                conn=conn,
            )

        conn.execute(
            "UPDATE paired_devices SET last_seen_at = ?, updated_at = ? WHERE id = ?",
            (now_iso, now_iso, dev_row["id"]),
        )
        conn.commit()
    except sqlite3.IntegrityError:
        conn.rollback()
        # Concurrent race fallback: fetch the winning row for (customer_key, dedup_fingerprint)
        existing = conn.execute(
            """
            SELECT sms_event_id, device_id, risk_level, evidence_strength,
                   alert_generated, received_at
            FROM sms_threat_events
            WHERE customer_key = ? AND dedup_fingerprint = ?
            """,
            (principal.customer_key, dedup_fingerprint),
        ).fetchone()
        if existing is not None:
            return {
                "sms_event_id": existing["sms_event_id"],
                "device_id": existing["device_id"],
                "deduplicated": True,
                "risk_level": existing["risk_level"],
                "evidence_strength": existing["evidence_strength"],
                "alert_generated": bool(existing["alert_generated"]),
                "received_at": existing["received_at"],
            }
        raise
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    return {
        "sms_event_id": sms_event_id,
        "device_id": principal.device_id,
        "deduplicated": False,
        "risk_level": risk_level,
        "evidence_strength": evidence_strength,
        "alert_generated": should_alert,
        "received_at": now_iso,
    }


def list_sms_threats(
    principal: CustomerPrincipal,
    *,
    limit: int = 25,
    cursor: str | None = None,
    risk_level: str | None = None,
    acknowledged: bool | None = None,
    device_id: str | None = None,
) -> dict:
    """List customer-owned SMS threat events ordered newest first with filtering and pagination."""
    owner = _owner(principal)
    if not 1 <= limit <= PAGE_MAX:
        raise InvalidSmsInputError("limit must be between 1 and 100.")
    offset = _cursor_offset(cursor)

    clauses = ["customer_key = ?"]
    params: list[object] = [owner]

    if risk_level is not None:
        normalized_risk = str(risk_level).strip().upper()
        if normalized_risk not in ALLOWED_RISK_LEVELS:
            raise InvalidSmsInputError("Unsupported risk_level filter.")
        clauses.append("risk_level = ?")
        params.append(normalized_risk)

    if acknowledged is not None:
        clauses.append("acknowledged = ?")
        params.append(1 if acknowledged else 0)

    if device_id is not None:
        clean_dev = str(device_id).strip()
        if not clean_dev:
            raise InvalidSmsInputError("device_id filter cannot be empty.")
        clauses.append("device_id = ?")
        params.append(clean_dev)

    where_sql = " AND ".join(clauses)
    query_params = [*params, limit + 1, offset]

    conn = _get_db_connection()
    try:
        rows = conn.execute(
            f"""
            SELECT sms_event_id, device_id, source_package, sender, sender_type,
                   redacted_message, message_timestamp, received_at, risk_level,
                   evidence_strength, detected_languages_json, code_mixed,
                   language_status, alert_generated, acknowledged, acknowledged_at
            FROM sms_threat_events
            WHERE {where_sql}
            ORDER BY received_at DESC, id DESC
            LIMIT ? OFFSET ?
            """,
            query_params,
        ).fetchall()
    finally:
        conn.close()

    has_more = len(rows) > limit
    items = [_serialize_sms_summary(row) for row in rows[:limit]]
    return {
        "items": items,
        "next_cursor": _cursor(offset + limit) if has_more else None,
        "has_more": has_more,
    }


def get_sms_threat(
    principal: CustomerPrincipal,
    sms_event_id: UUID | str,
) -> dict:
    """Retrieve a single customer-owned SMS threat event detail."""
    owner = _owner(principal)
    conn = _get_db_connection()
    try:
        row = conn.execute(
            """
            SELECT sms_event_id, device_id, source_package, sender, sender_type,
                   redacted_message, message_timestamp, received_at, risk_level,
                   evidence_strength, indicators_json, explanation, warning,
                   recommendations_json, detected_languages_json, code_mixed,
                   language_status, engine_version, alert_generated,
                   acknowledged, acknowledged_at
            FROM sms_threat_events
            WHERE sms_event_id = ? AND customer_key = ?
            """,
            (str(sms_event_id), owner),
        ).fetchone()
    finally:
        conn.close()

    if row is None:
        raise SmsThreatNotFoundError("SMS threat event not found.")
    return _serialize_sms_detail(row)


def acknowledge_sms_threat(
    principal: CustomerPrincipal,
    sms_event_id: UUID | str,
) -> dict:
    """Idempotently acknowledge a customer-owned SMS threat event."""
    owner = _owner(principal)
    event_id_str = str(sms_event_id)
    now_iso = _now_utc().isoformat()

    conn = _get_db_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            """
            SELECT id, acknowledged, acknowledged_at
            FROM sms_threat_events
            WHERE sms_event_id = ? AND customer_key = ?
            """,
            (event_id_str, owner),
        ).fetchone()
        if row is None:
            conn.rollback()
            raise SmsThreatNotFoundError("SMS threat event not found.")

        if not row["acknowledged"]:
            conn.execute(
                """
                UPDATE sms_threat_events
                SET acknowledged = 1, acknowledged_at = ?
                WHERE id = ?
                """,
                (now_iso, row["id"]),
            )
            conn.execute(
                """
                UPDATE notifications
                SET is_read = 1
                WHERE customer_key = ? AND event_ref = ?
                """,
                (owner, f"sms-threat-{event_id_str}"),
            )

        updated = conn.execute(
            """
            SELECT sms_event_id, device_id, source_package, sender, sender_type,
                   redacted_message, message_timestamp, received_at, risk_level,
                   evidence_strength, indicators_json, explanation, warning,
                   recommendations_json, detected_languages_json, code_mixed,
                   language_status, engine_version, alert_generated,
                   acknowledged, acknowledged_at
            FROM sms_threat_events
            WHERE id = ?
            """,
            (row["id"],),
        ).fetchone()
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    return _serialize_sms_detail(updated)
