"""Demo-only signed-cookie authentication for the versioned API."""

import base64
import hashlib
import hmac
import json
import os
import secrets
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
    RegisterSendOtpRequest,
    RegisterSendOtpResponse,
    RegisterVerifyOtpRequest,
    SigninAuditLogEntry,
)
from services.banking_service import get_customer_profile_by_key
from services.database import DEMO_CUSTOMER_KEYS, get_connection
from services.email_service import send_verification_email, verify_email_otp
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
    if customer_key not in DEMO_CUSTOMER_KEY_ALLOWLIST and get_customer_profile_by_key(customer_key) is None:
        raise ValueError("Customer is not available in authentication.")

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
    customer_valid = (
        customer_key in DEMO_CUSTOMER_KEY_ALLOWLIST
        or (isinstance(customer_key, str) and get_customer_profile_by_key(customer_key) is not None)
    )
    if (
        payload.get("iss") != SESSION_ISSUER
        or payload.get("aud") != SESSION_AUDIENCE
        or not isinstance(customer_key, str)
        or not customer_valid
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
        if not customer_key:
            record_signin_audit_log(customer["customer_id"], email, "FAILED", client_ip, raw_ua, platform)
            raise HTTPException(status_code=403, detail="Customer account record is missing public key.")

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
            auth_mode="REGISTERED" if customer_key not in DEMO_CUSTOMER_KEY_ALLOWLIST else "DEMO",
        )
    finally:
        conn.close()


@router.post("/register/send-otp", response_model=RegisterSendOtpResponse)
def register_send_otp(payload: RegisterSendOtpRequest):
    """Validate registration draft and send an email verification OTP via SMTP."""
    clean_email = payload.email.strip().lower()

    # Check if email is already registered
    conn = get_connection()
    try:
        existing = conn.execute(
            "SELECT customer_id FROM customers WHERE LOWER(email) = LOWER(?)",
            (clean_email,),
        ).fetchone()
        if existing:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="An account with this email address already exists. Please sign in instead.",
            )
    finally:
        conn.close()

    try:
        result = send_verification_email(
            email=clean_email,
            customer_name=payload.name.strip(),
        )
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(exc),
        )

    return RegisterSendOtpResponse(
        status="otp_sent",
        email=clean_email,
        message=result.get("message", "Verification code dispatched."),
        dev_otp=result.get("dev_otp"),
    )


@router.post("/register/verify-otp", response_model=DemoSessionResponse)
def register_verify_otp(
    payload: RegisterVerifyOtpRequest,
    request: Request,
    response: Response,
):
    """Verify email OTP, create customer, provision initial account, and start authenticated session."""
    clean_email = payload.email.strip().lower()
    raw_ua = request.headers.get("user-agent", "Unknown")
    platform = _parse_device_platform(raw_ua)
    client_ip = request.client.host if request.client else "127.0.0.1"
    if client_ip in {"127.0.0.1", "::1", "localhost"}:
        client_ip = "127.0.0.1 (Local Session)"

    # Verify the OTP
    is_valid, message = verify_email_otp(clean_email, payload.otp_code)
    if not is_valid:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=message,
        )

    conn = get_connection()
    try:
        # Check email uniqueness inside transaction
        existing = conn.execute(
            "SELECT customer_id FROM customers WHERE LOWER(email) = LOWER(?)",
            (clean_email,),
        ).fetchone()
        if existing:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="An account with this email address is already registered.",
            )

        customer_key = f"user-{uuid4().hex[:10]}"
        hashed_pwd = hash_password(payload.password)
        clean_name = payload.name.strip()
        clean_phone = payload.phone.strip()
        income = float(payload.monthly_income)

        cursor = conn.execute(
            """
            INSERT INTO customers (customer_key, name, email, phone, monthly_income, password_hash)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (customer_key, clean_name, clean_email, clean_phone, income, hashed_pwd),
        )
        customer_id = cursor.lastrowid

        # Determine account type and initial balance
        acct_type = payload.account_type.upper()
        if acct_type not in {"SAVINGS", "SALARY", "CURRENT"}:
            acct_type = "SAVINGS"

        initial_balances = {
            "SAVINGS": 25000.0,
            "SALARY": 50000.0,
            "CURRENT": 100000.0,
        }
        balance = initial_balances.get(acct_type, 25000.0)
        account_number = f"VS{secrets.randbelow(90000000) + 10000000}"

        cursor_acct = conn.execute(
            """
            INSERT INTO accounts (customer_id, account_number, account_type, balance, status)
            VALUES (?, ?, ?, ?, 'ACTIVE')
            """,
            (customer_id, account_number, acct_type, balance),
        )
        account_id = cursor_acct.lastrowid

        # Provision a virtual debit card for the account
        last_four = f"{secrets.randbelow(9000) + 1000}"
        conn.execute(
            """
            INSERT INTO cards (account_id, card_type, last_four, status, expiry_date)
            VALUES (?, 'DEBIT', ?, 'ACTIVE', '12/30')
            """,
            (account_id, last_four),
        )

        # Welcome notification
        now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
        notif_id = str(uuid4())
        event_ref = f"welcome-{customer_key}"
        welcome_title = f"Welcome to VaaniShield, {clean_name}!"
        welcome_msg = (
            f"Your new {acct_type.title()} Account ({account_number}) is active with an opening "
            f"balance of ₹{balance:,.2f}. Email verification was completed successfully."
        )
        try:
            conn.execute(
                """
                INSERT OR IGNORE INTO notifications (
                    notification_id, customer_key, category, title, message, event_ref, is_read, created_at
                )
                VALUES (?, ?, 'banking_alert', ?, ?, ?, 0, ?)
                """,
                (notif_id, customer_key, welcome_title, welcome_msg, event_ref, now_iso),
            )
        except Exception:
            pass

        conn.commit()
        record_signin_audit_log(customer_id, clean_email, "SUCCESS (NEW REGISTRATION)", client_ip, raw_ua, platform)

        issued_at = int(time.time())
        expires_at = issued_at + SESSION_TTL_SECONDS
        token = _encode_session_token(customer_key, issued_at, expires_at)
        _set_session_cookie(response, token)

        return DemoSessionResponse(
            customer_key=customer_key,
            display_name=clean_name,
            expires_at=datetime.fromtimestamp(expires_at, timezone.utc),
            auth_mode="REGISTERED",
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
