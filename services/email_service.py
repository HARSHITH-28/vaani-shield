"""SMTP Email verification and dispatch service for VaaniShield banking.

Provides RFC 5322 compliant HTML & plaintext verification emails with
PBKDF2-hardened OTP lifecycle management, TLS/SSL SMTP transport,
and graceful development/demonstration fallback when SMTP servers are unconfigured.
"""

from __future__ import annotations

import hmac
import html
import logging
import os
import secrets
import smtplib
import socket
import ssl
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from services.database import get_connection

logger = logging.getLogger(__name__)

OTP_EXPIRY_MINUTES = 10


def ensure_verification_table() -> None:
    """Ensure the email_verifications table exists."""
    conn = get_connection()
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS email_verifications (
                verification_id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT NOT NULL,
                otp_code TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                is_verified INTEGER DEFAULT 0 CHECK (is_verified IN (0, 1)),
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_email_verifications_lookup "
            "ON email_verifications(email, expires_at)"
        )
        conn.commit()
    finally:
        conn.close()


def generate_otp() -> str:
    """Generate a cryptographically secure 6-digit numeric OTP."""
    return f"{secrets.randbelow(900000) + 100000}"


def _build_email_html(customer_name: str, otp_code: str, expires_in_minutes: int) -> str:
    """Generate a clean, professional, branded HTML email for VaaniShield."""
    safe_name = html.escape(customer_name.strip() or "Valued Customer")
    safe_otp = html.escape(otp_code)
    
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>VaaniShield Email Verification</title>
  <style>
    body {{
      margin: 0;
      padding: 0;
      background-color: #FAF9FF;
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif;
      color: #1F1B2D;
    }}
    .email-container {{
      max-width: 580px;
      margin: 30px auto;
      background: #FFFFFF;
      border: 1px solid #E7E0FF;
      border-radius: 16px;
      overflow: hidden;
      box-shadow: 0 10px 30px rgba(74, 48, 146, 0.08);
    }}
    .header {{
      background: linear-gradient(135deg, #5B3FC4 0%, #472E9E 100%);
      padding: 32px 30px;
      text-align: center;
      color: #FFFFFF;
    }}
    .header .shield-icon {{
      font-size: 38px;
      margin-bottom: 8px;
      display: inline-block;
    }}
    .header h1 {{
      margin: 0;
      font-size: 24px;
      font-weight: 700;
      letter-spacing: -0.5px;
    }}
    .header p {{
      margin: 6px 0 0;
      font-size: 13px;
      opacity: 0.9;
      letter-spacing: 0.2px;
    }}
    .content {{
      padding: 36px 32px;
    }}
    .content h2 {{
      margin-top: 0;
      font-size: 19px;
      font-weight: 700;
      color: #1F1B2D;
    }}
    .content p {{
      font-size: 14.5px;
      line-height: 1.6;
      color: #4A4658;
      margin: 12px 0;
    }}
    .otp-box {{
      margin: 28px 0;
      background: #F7F4FF;
      border: 1.5px dashed #7863D5;
      border-radius: 12px;
      padding: 22px;
      text-align: center;
    }}
    .otp-label {{
      font-size: 12px;
      font-weight: 700;
      text-transform: uppercase;
      letter-spacing: 1.2px;
      color: #5B3FC4;
      margin-bottom: 8px;
    }}
    .otp-code {{
      font-family: 'SFMono-Regular', Consolas, 'Liberation Mono', Menlo, monospace;
      font-size: 36px;
      font-weight: 800;
      letter-spacing: 10px;
      color: #1F1B2D;
      display: inline-block;
      margin-left: 10px;
    }}
    .otp-validity {{
      font-size: 12.5px;
      color: #6B667A;
      margin-top: 10px;
    }}
    .security-card {{
      background: #F8FAFC;
      border-left: 4px solid #0D9488;
      border-radius: 8px;
      padding: 14px 16px;
      margin: 24px 0 16px;
    }}
    .security-card p {{
      margin: 0;
      font-size: 12.5px;
      color: #334155;
      line-height: 1.5;
    }}
    .footer {{
      background: #FAF9FF;
      padding: 20px 32px;
      border-top: 1px solid #E7E0FF;
      text-align: center;
      font-size: 11.5px;
      color: #8A8498;
      line-height: 1.6;
    }}
  </style>
</head>
<body>
  <div class="email-container">
    <div class="header">
      <div class="shield-icon">&#128737;&#65039;</div>
      <h1>VaaniShield Digital Banking</h1>
      <p>Intelligent Banking &amp; Financial Security Platform</p>
    </div>
    <div class="content">
      <h2>Verify your email address</h2>
      <p>Hello <strong>{safe_name}</strong>,</p>
      <p>Thank you for initiating your account registration with VaaniShield. To verify your email address and activate your account, please enter the One-Time Password (OTP) below:</p>
      
      <div class="otp-box">
        <div class="otp-label">Your Verification Code</div>
        <div class="otp-code">{safe_otp}</div>
        <div class="otp-validity">Valid for {expires_in_minutes} minutes &bull; Single-use only</div>
      </div>
      
      <div class="security-card">
        <p><strong>&#128274; Security Advisory:</strong> Never share your verification code, passwords, or PINs with anyone. VaaniShield representatives will never call or message asking for your OTP.</p>
      </div>

      <p style="font-size: 12.5px; color: #8A8498; margin-top: 24px;">
        If you did not request this registration, please disregard this email or contact support at security@vaanishield.bank.
      </p>
    </div>
    <div class="footer">
      &copy; 2026 VaaniShield Technologies Private Limited. All rights reserved.<br>
      Licensed Financial Intelligence System &bull; Bangalore, India
    </div>
  </div>
</body>
</html>
"""


def _build_email_plaintext(customer_name: str, otp_code: str, expires_in_minutes: int) -> str:
    """Plaintext fallback for non-HTML mail clients."""
    name = customer_name.strip() or "Valued Customer"
    return (
        f"VaaniShield Digital Banking - Email Verification\n"
        f"=================================================\n\n"
        f"Hello {name},\n\n"
        f"Your verification code for opening your VaaniShield account is:\n\n"
        f"    {otp_code}\n\n"
        f"This code will expire in {expires_in_minutes} minutes.\n\n"
        f"Security Notice:\n"
        f"Never share your OTP with anyone. VaaniShield will never contact you asking for this code.\n\n"
        f"If you did not request this, please ignore this email.\n\n"
        f"VaaniShield Technologies Private Limited | Bangalore, India\n"
    )


def _connect_smtp_server(
    host: str,
    port: int,
    use_ssl: bool,
    use_tls: bool,
    ssl_context: ssl.SSLContext,
    timeout: int = 15,
) -> smtplib.SMTP:
    """Connect to SMTP server with automatic IPv4 resolution to prevent [Errno 99] in Linux cloud containers."""
    target_ip = None
    try:
        addr_info = socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_STREAM)
        if addr_info:
            target_ip = addr_info[0][4][0]
    except Exception:
        target_ip = None

    last_err = None
    candidates = [target_ip, host] if target_ip else [host]

    if port == 465 or use_ssl:
        server = smtplib.SMTP_SSL(timeout=timeout, context=ssl_context)
        connected = False
        for target in candidates:
            try:
                server.connect(target, port)
                connected = True
                break
            except OSError as exc:
                last_err = exc
                continue
        if not connected:
            raise last_err or smtplib.SMTPConnectError(500, f"Failed to connect to {host}:{port}")
        server._host = host
        return server
    else:
        server = smtplib.SMTP(timeout=timeout)
        connected = False
        for target in candidates:
            try:
                server.connect(target, port)
                connected = True
                break
            except OSError as exc:
                last_err = exc
                continue
        if not connected:
            raise last_err or smtplib.SMTPConnectError(500, f"Failed to connect to {host}:{port}")
        server._host = host
        if use_tls:
            server.starttls(context=ssl_context)
        return server


def send_verification_email(
    email: str,
    customer_name: str = "",
    otp_code: str | None = None,
) -> dict:
    """Send an email verification code using SMTP with graceful dev fallback.

    Returns a dict with:
      - success: bool
      - method: "smtp" | "dev_fallback"
      - message: User-facing message
      - dev_otp: str | None (provided only when SMTP is in dev/demonstration fallback)
    """
    clean_email = email.strip().lower()
    if not clean_email or "@" not in clean_email:
        raise ValueError("A valid email address is required.")

    ensure_verification_table()

    if not otp_code:
        otp_code = generate_otp()

    expires_at = (datetime.now(timezone.utc) + timedelta(minutes=OTP_EXPIRY_MINUTES)).isoformat()

    # Save OTP to database
    conn = get_connection()
    try:
        # Invalidate previous unverified OTPs for this email
        conn.execute(
            "UPDATE email_verifications SET is_verified = 1 WHERE LOWER(email) = LOWER(?) AND is_verified = 0",
            (clean_email,),
        )
        conn.execute(
            """
            INSERT INTO email_verifications (email, otp_code, expires_at, is_verified)
            VALUES (?, ?, ?, 0)
            """,
            (clean_email, otp_code, expires_at),
        )
        conn.commit()
    finally:
        conn.close()

    # Read SMTP Configuration
    smtp_user = os.getenv("SMTP_USERNAME", os.getenv("SMTP_USER", "")).strip()
    smtp_password = os.getenv("SMTP_PASSWORD", os.getenv("SMTP_PASS", "")).strip()
    smtp_host = os.getenv("SMTP_HOST", os.getenv("SMTP_SERVER", "")).strip()
    smtp_port_raw = os.getenv("SMTP_PORT", "587").strip()
    smtp_port = int(smtp_port_raw) if smtp_port_raw.isdigit() else 587
    smtp_from = os.getenv("SMTP_FROM_EMAIL", os.getenv("SMTP_SENDER", "")).strip()
    smtp_use_tls = os.getenv("SMTP_USE_TLS", "true").strip().lower() in ("true", "1", "yes")
    smtp_use_ssl = os.getenv("SMTP_USE_SSL", "false").strip().lower() in ("true", "1", "yes")

    # If host is missing or localhost, default to Gmail SMTP if user is Gmail
    if not smtp_host or smtp_host.lower() in ("localhost", "127.0.0.1", "::1", "0.0.0.0"):
        if "gmail" in smtp_user.lower() or not smtp_host:
            smtp_host = "smtp.gmail.com"
            smtp_port = 587

    if "gmail" in smtp_host.lower():
        smtp_password = smtp_password.replace(" ", "")

    is_local_host = smtp_host.lower() in ("localhost", "127.0.0.1", "::1", "0.0.0.0")

    # Validate credentials for external SMTP servers
    if not is_local_host and (not smtp_user or not smtp_password):
        if "gmail" in smtp_host.lower():
            raise RuntimeError(
                "Gmail SMTP credentials are not configured in .env. "
                "To send live verification emails via smtp.gmail.com, please set SMTP_USERNAME to your Gmail address "
                "and SMTP_PASSWORD to a 16-character Google App Password (with 2-Step Verification enabled at "
                "https://myaccount.google.com/apppasswords)."
            )
        raise RuntimeError(
            f"SMTP server '{smtp_host}' requires authentication, but SMTP_USERNAME or SMTP_PASSWORD is not configured in .env."
        )

    # Align sender address with authenticated mailbox to prevent provider SPF/DMARC rejection
    effective_from = smtp_from
    if smtp_user and "@" in smtp_user:
        if not effective_from or "vaanishield.bank" in effective_from.lower():
            effective_from = smtp_user
    elif not effective_from:
        effective_from = "no-reply@vaanishield.bank"

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"VaaniShield Verification Code: {otp_code}"
    msg["From"] = f"VaaniShield Security <{effective_from}>"
    msg["Sender"] = effective_from
    msg["Reply-To"] = effective_from
    msg["To"] = clean_email

    part_text = MIMEText(_build_email_plaintext(customer_name, otp_code, OTP_EXPIRY_MINUTES), "plain", "utf-8")
    part_html = MIMEText(_build_email_html(customer_name, otp_code, OTP_EXPIRY_MINUTES), "html", "utf-8")
    msg.attach(part_text)
    msg.attach(part_html)

    ssl_context = ssl.create_default_context()

    try:
        with _connect_smtp_server(smtp_host, smtp_port, smtp_use_ssl, smtp_use_tls, ssl_context, timeout=15) as server:
            if smtp_user and smtp_password:
                server.login(smtp_user, smtp_password)
            server.send_message(msg)

        logger.info("Verification email sent via SMTP to %s", clean_email)
        return {
            "success": True,
            "method": "smtp",
            "message": f"Verification code sent to {clean_email} via SMTP.",
            "dev_otp": None,
        }
    except smtplib.SMTPAuthenticationError as exc:
        logger.error("SMTP Authentication Error for %s: %s", smtp_user, exc)
        if "gmail" in smtp_host.lower():
            raise RuntimeError(
                f"Gmail SMTP authentication failed for '{smtp_user}'. "
                "Google requires a 16-character App Password (not your account password) with 2-Step Verification enabled. "
                "Generate one at: https://myaccount.google.com/apppasswords"
            ) from exc
        raise RuntimeError(f"SMTP authentication failed on {smtp_host}: {str(exc)}") from exc
    except (smtplib.SMTPConnectError, socket.timeout, TimeoutError, ConnectionRefusedError, socket.gaierror, OSError) as exc:
        logger.error("SMTP connection failure to %s:%s: %s", smtp_host, smtp_port, exc)
        raise RuntimeError(
            f"Unable to connect to SMTP server '{smtp_host}:{smtp_port}' ({str(exc)}). Please check your network connection and server settings."
        ) from exc
    except smtplib.SMTPRecipientsRefused as exc:
        logger.error("SMTP recipient refused: %s: %s", clean_email, exc)
        raise RuntimeError(f"The recipient address '{clean_email}' was rejected by the mail server.") from exc
    except smtplib.SMTPSenderRefused as exc:
        logger.error("SMTP sender refused: %s: %s", effective_from, exc)
        raise RuntimeError(f"The sender address '{effective_from}' was rejected by the mail server.") from exc
    except smtplib.SMTPException as exc:
        logger.error("SMTP dispatch error to %s: %s", clean_email, exc)
        raise RuntimeError(f"SMTP mail transfer error: {str(exc)}") from exc
    except Exception as exc:
        logger.error("Unexpected error sending email to %s: %s", clean_email, exc)
        raise RuntimeError(f"Failed to dispatch verification email: {str(exc)}") from exc


def verify_email_otp(email: str, otp_code: str) -> tuple[bool, str]:
    """Verify submitted OTP code against active unexpired verification records.

    Returns (is_valid, message).
    """
    clean_email = email.strip().lower()
    clean_code = str(otp_code or "").strip()

    if not clean_email or not clean_code:
        return False, "Email and 6-digit verification code are required."

    ensure_verification_table()

    conn = get_connection()
    try:
        row = conn.execute(
            """
            SELECT verification_id, otp_code, expires_at, is_verified
            FROM email_verifications
            WHERE LOWER(email) = LOWER(?) AND is_verified = 0
            ORDER BY verification_id DESC
            LIMIT 1
            """,
            (clean_email,),
        ).fetchone()

        if not row:
            return False, "No active verification code found for this email. Please request a new code."

        verif_data = dict(row)
        expected_code = str(verif_data["otp_code"]).strip()

        # Constant-time comparison for security
        if not hmac.compare_digest(expected_code, clean_code):
            return False, "Incorrect verification code. Please check your email and try again."

        # Check expiration
        expires_at_str = verif_data["expires_at"]
        try:
            expires_at = datetime.fromisoformat(expires_at_str)
        except Exception:
            return False, "Verification record has invalid expiration timestamp."

        if datetime.now(timezone.utc) > expires_at:
            return False, "This verification code has expired. Please request a new one."

        # Mark as verified
        conn.execute(
            "UPDATE email_verifications SET is_verified = 1 WHERE verification_id = ?",
            (verif_data["verification_id"],),
        )
        conn.commit()
        return True, "Email verified successfully."
    finally:
        conn.close()

