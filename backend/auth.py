"""Demo-only signed-cookie authentication for the versioned API."""

import base64
import hashlib
import hmac
import json
import os
import time
from datetime import datetime, timezone, timedelta
from uuid import UUID, uuid4

from dotenv import load_dotenv
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status

from backend.api_models import (
    DemoLoginRequest,
    DemoLogoutResponse,
    DemoSessionRequest,
    DemoSessionResponse,
    SigninAuditLogEntry,
)
from services.banking_service import get_customer_profile_by_key
from services.database import DEMO_CUSTOMER_KEYS, get_connection
from services.principal import CustomerPrincipal


load_dotenv()

router = APIRouter(prefix="/api/v1/auth/demo", tags=["Demo Authentication"])

SESSION_COOKIE_NAME = "vaanishield_demo_session"
# Cover both the versioned endpoints and authenticated legacy dashboard routes.
SESSION_COOKIE_PATH = "/api"
LEGACY_SESSION_COOKIE_PATH = "/api/v1"
SESSION_TTL_SECONDS = 60 * 60
SESSION_ISSUER = "vaanishield-demo"
SESSION_AUDIENCE = "vaanishield-api-v1"
DEMO_CUSTOMER_KEY_ALLOWLIST = frozenset(DEMO_CUSTOMER_KEYS.values())


def _signing_secret() -> bytes:
    secret = os.getenv("VAANISHIELD_DEMO_SESSION_SECRET", "")
    if (
        len(secret.encode("utf-8")) < 32
        or secret.strip().lower().startswith("replace_with")
    ):
        raise RuntimeError("Demo session authentication is not configured.")
    return secret.encode("utf-8")


def _b64url_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64url_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.b64decode(
        value + padding,
        altchars=b"-_",
        validate=True,
    )


def _encode_session_token(
    customer_key: str,
    issued_at: int,
    expires_at: int,
    session_id: UUID | None = None,
) -> str:
    """Create a signed token. Kept small and local; no JWT dependency needed."""
    if customer_key not in DEMO_CUSTOMER_KEY_ALLOWLIST:
        raise ValueError("Customer is not available in demo authentication.")

    header = {"alg": "HS256", "typ": "JWT"}
    payload = {
        "iss": SESSION_ISSUER,
        "aud": SESSION_AUDIENCE,
        "sub": customer_key,
        "iat": issued_at,
        "exp": expires_at,
        "jti": str(session_id or uuid4()),
    }
    encoded_header = _b64url_encode(
        json.dumps(header, separators=(",", ":"), sort_keys=True).encode("utf-8")
    )
    encoded_payload = _b64url_encode(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    )
    signing_input = f"{encoded_header}.{encoded_payload}".encode("ascii")
    signature = hmac.new(_signing_secret(), signing_input, hashlib.sha256).digest()
    return f"{encoded_header}.{encoded_payload}.{_b64url_encode(signature)}"


def _decode_session_token(token: str) -> CustomerPrincipal:
    if len(token) > 4096:
        raise ValueError("Invalid session.")

    parts = token.split(".")
    if len(parts) != 3 or any(not part for part in parts):
        raise ValueError("Invalid session.")

    encoded_header, encoded_payload, encoded_signature = parts
    signing_input = f"{encoded_header}.{encoded_payload}".encode("ascii")
    supplied_signature = _b64url_decode(encoded_signature)
    expected_signature = hmac.new(
        _signing_secret(), signing_input, hashlib.sha256
    ).digest()
    if not hmac.compare_digest(supplied_signature, expected_signature):
        raise ValueError("Invalid session.")

    header = json.loads(_b64url_decode(encoded_header))
    payload = json.loads(_b64url_decode(encoded_payload))
    if header != {"alg": "HS256", "typ": "JWT"}:
        raise ValueError("Invalid session.")
    if not isinstance(payload, dict):
        raise ValueError("Invalid session.")

    customer_key = payload.get("sub")
    issued_at = payload.get("iat")
    expires_at = payload.get("exp")
    if (
        payload.get("iss") != SESSION_ISSUER
        or payload.get("aud") != SESSION_AUDIENCE
        or not isinstance(customer_key, str)
        or customer_key not in DEMO_CUSTOMER_KEY_ALLOWLIST
        or type(issued_at) is not int
        or type(expires_at) is not int
    ):
        raise ValueError("Invalid session.")

    now = int(time.time())
    if (
        issued_at > now + 30
        or expires_at <= now
        or expires_at <= issued_at
        or expires_at - issued_at > SESSION_TTL_SECONDS
    ):
        raise ValueError("Invalid session.")

    try:
        session_id = UUID(payload.get("jti", ""))
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValueError("Invalid session.") from exc

    # Re-check current server-side identity so deleted/disabled demo rows stop
    # authenticating even while an otherwise valid cookie remains unexpired.
    if get_customer_profile_by_key(customer_key) is None:
        raise ValueError("Invalid session.")

    return CustomerPrincipal(
        customer_key=customer_key,
        session_id=session_id,
        issued_at=datetime.fromtimestamp(issued_at, timezone.utc),
        expires_at=datetime.fromtimestamp(expires_at, timezone.utc),
    )


def get_authenticated_principal(request: Request) -> CustomerPrincipal:
    """FastAPI dependency resolving the signed cookie to server identity."""
    token = request.cookies.get(SESSION_COOKIE_NAME)
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required.",
        )

    try:
        return _decode_session_token(token)
    except RuntimeError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Demo session authentication is not configured.",
        ) from exc
    except (ValueError, UnicodeError, json.JSONDecodeError, base64.binascii.Error):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired demo session.",
        ) from None


def _cookie_secure() -> bool:
    return os.getenv("VAANISHIELD_DEMO_COOKIE_SECURE", "false").strip().lower() == "true"


def _set_session_cookie(response: Response, token: str) -> None:
    # Retire cookies issued by the previous /api/v1-only scope. Otherwise a
    # browser may send duplicate same-name cookies to versioned endpoints.
    response.delete_cookie(
        key=SESSION_COOKIE_NAME,
        path=LEGACY_SESSION_COOKIE_PATH,
        secure=_cookie_secure(),
        httponly=True,
        samesite="strict",
    )
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=token,
        max_age=SESSION_TTL_SECONDS,
        httponly=True,
        secure=_cookie_secure(),
        samesite="strict",
        path=SESSION_COOKIE_PATH,
    )
    response.headers["Cache-Control"] = "no-store"


@router.post("/session", response_model=DemoSessionResponse)
def create_demo_session(payload: DemoSessionRequest, response: Response):
    """Start a session for one of the fixed synthetic demo customers."""
    customer_key = payload.customer_key
    if customer_key not in DEMO_CUSTOMER_KEY_ALLOWLIST:
        raise HTTPException(status_code=404, detail="Demo profile not found.")

    profile = get_customer_profile_by_key(customer_key)
    if profile is None:
        raise HTTPException(status_code=404, detail="Demo profile not found.")

    issued_at = int(time.time())
    expires_at = issued_at + SESSION_TTL_SECONDS
    try:
        token = _encode_session_token(customer_key, issued_at, expires_at)
    except RuntimeError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Demo session authentication is not configured.",
        ) from exc

    _set_session_cookie(response, token)
    return DemoSessionResponse(
        customer_key=customer_key,
        display_name=profile["name"],
        expires_at=datetime.fromtimestamp(expires_at, timezone.utc),
    )


@router.get("/session", response_model=DemoSessionResponse)
def read_demo_session(
    response: Response,
    principal: CustomerPrincipal = Depends(get_authenticated_principal),
):
    profile = get_customer_profile_by_key(principal.customer_key)
    if profile is None:
        raise HTTPException(status_code=401, detail="Invalid or expired demo session.")

    response.headers["Cache-Control"] = "no-store"
    return DemoSessionResponse(
        customer_key=principal.customer_key,
        display_name=profile["name"],
        expires_at=principal.expires_at,
    )


@router.delete("/session", response_model=DemoLogoutResponse)
def delete_demo_session(response: Response):
    response.delete_cookie(
        key=SESSION_COOKIE_NAME,
        path=SESSION_COOKIE_PATH,
        secure=_cookie_secure(),
        httponly=True,
        samesite="strict",
    )
    response.delete_cookie(
        key=SESSION_COOKIE_NAME,
        path=LEGACY_SESSION_COOKIE_PATH,
        secure=_cookie_secure(),
        httponly=True,
        samesite="strict",
    )
    response.headers["Cache-Control"] = "no-store"
    return DemoLogoutResponse()


def hash_password(password: str, salt: bytes | None = None) -> str:
    """Hash password using PBKDF2-SHA256 with 100,000 iterations."""
    if salt is None:
        salt = os.urandom(16)
    key = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 100_000)
    return f"pbkdf2_sha256$100000${salt.hex()}${key.hex()}"


def verify_password(password: str, password_hash: str | None) -> bool:
    """Verify plain password against PBKDF2-SHA256 hash using constant-time comparison."""
    if not password or not password_hash:
        return False
    try:
        parts = password_hash.split("$")
        if len(parts) != 4 or parts[0] != "pbkdf2_sha256":
            return False
        iterations = int(parts[1])
        salt = bytes.fromhex(parts[2])
        expected_key = bytes.fromhex(parts[3])
        key = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
        return hmac.compare_digest(key, expected_key)
    except Exception:
        return False


def _parse_device_platform(user_agent: str) -> str:
    """Parse a high-level operating system and browser name without invasive telemetry."""
    ua = (user_agent or "").lower()
    os_name = "Desktop"
    if "windows" in ua:
        os_name = "Windows"
    elif "macintosh" in ua or "mac os" in ua:
        os_name = "macOS"
    elif "android" in ua:
        os_name = "Android"
    elif "iphone" in ua or "ipad" in ua:
        os_name = "iOS"
    elif "linux" in ua:
        os_name = "Linux"

    browser = "Browser"
    if "edg/" in ua:
        browser = "Microsoft Edge"
    elif "chrome/" in ua and "edg/" not in ua:
        browser = "Chrome"
    elif "safari/" in ua and "chrome/" not in ua:
        browser = "Safari"
    elif "firefox/" in ua:
        browser = "Firefox"

    return f"{os_name} · {browser}"


def record_signin_audit_log(
    customer_id: int | None,
    email: str,
    login_result: str,
    ip_address: str,
    user_agent: str,
    device_platform: str,
) -> None:
    """Store an audit entry for sign-in attempts. Never store passwords."""
    tz_ist = timezone(timedelta(hours=5, minutes=30))
    now_utc = datetime.now(timezone.utc)
    ts_utc = now_utc.isoformat()
    ts_ist = now_utc.astimezone(tz_ist).strftime("%d %b %Y, %I:%M %p IST")

    conn = get_connection()
    try:
        conn.execute("""
            INSERT INTO signin_audit_logs (
                customer_id, email_attempted, timestamp_utc, timestamp_ist,
                login_result, ip_address, user_agent, device_platform
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (customer_id, email, ts_utc, ts_ist, login_result, ip_address, user_agent[:255], device_platform[:100]))
        conn.commit()
    except Exception:
        pass
    finally:
        conn.close()


def get_signin_audit_logs(customer_id: int, limit: int = 15) -> list[dict]:
    """Retrieve verified sign-in audit history for the authenticated customer."""
    conn = get_connection()
    try:
        rows = conn.execute("""
            SELECT
                log_id,
                customer_id,
                email_attempted,
                timestamp_utc,
                timestamp_ist,
                login_result,
                ip_address,
                user_agent,
                device_platform
            FROM signin_audit_logs
            WHERE customer_id = ?
            ORDER BY log_id DESC
            LIMIT ?
        """, (customer_id, limit)).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


@router.post("/login", response_model=DemoSessionResponse)
def demo_login(payload: DemoLoginRequest, request: Request, response: Response):
    """Authenticate demo customer using email and password, logging the attempt."""
    email = payload.email.strip().lower()
    raw_ua = request.headers.get("user-agent", "Unknown")
    platform = _parse_device_platform(raw_ua)
    client_ip = request.client.host if request.client else "127.0.0.1"
    if client_ip in {"127.0.0.1", "::1", "localhost"}:
        client_ip = "127.0.0.1 (Local Session)"

    conn = get_connection()
    try:
        row = conn.execute("""
            SELECT customer_id, customer_key, name, email, password_hash
            FROM customers
            WHERE LOWER(email) = LOWER(?)
        """, (email,)).fetchone()

        if not row:
            record_signin_audit_log(None, email, "FAILED", client_ip, raw_ua, platform)
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid email or password.",
            )

        customer = dict(row)
        pw_hash = customer.get("password_hash")
        if not pw_hash or not verify_password(payload.password, pw_hash):
            record_signin_audit_log(customer["customer_id"], email, "FAILED", client_ip, raw_ua, platform)
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid email or password.",
            )

        customer_key = customer["customer_key"]
        if customer_key not in DEMO_CUSTOMER_KEY_ALLOWLIST:
            record_signin_audit_log(customer["customer_id"], email, "FAILED", client_ip, raw_ua, platform)
            raise HTTPException(status_code=403, detail="Customer not permitted for demo login.")

        record_signin_audit_log(customer["customer_id"], email, "SUCCESS", client_ip, raw_ua, platform)

        issued_at = int(time.time())
        expires_at = issued_at + SESSION_TTL_SECONDS
        try:
            token = _encode_session_token(customer_key, issued_at, expires_at)
        except RuntimeError as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Demo session authentication is not configured.",
            ) from exc

        _set_session_cookie(response, token)
        return DemoSessionResponse(
            customer_key=customer_key,
            display_name=customer["name"],
            expires_at=datetime.fromtimestamp(expires_at, timezone.utc),
        )
    finally:
        conn.close()


@router.get("/security-activity", response_model=list[SigninAuditLogEntry])
def read_security_activity(
    principal: CustomerPrincipal = Depends(get_authenticated_principal),
):
    """Return sign-in audit history for the authenticated customer."""
    profile = get_customer_profile_by_key(principal.customer_key)
    if profile is None:
        raise HTTPException(status_code=401, detail="Invalid or expired demo session.")
    return get_signin_audit_logs(profile["customer_id"], limit=15)
