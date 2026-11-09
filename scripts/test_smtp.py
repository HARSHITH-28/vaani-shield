"""SMTP Connectivity and Diagnostic Tool for VaaniShield.

Usage:
    python scripts/test_smtp.py [recipient_email]

Examples:
    python scripts/test_smtp.py
    python scripts/test_smtp.py user@example.com
"""

import os
import sys
import smtplib
import ssl
import socket
from pathlib import Path
from dotenv import load_dotenv

# Load .env from project root
env_path = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(dotenv_path=env_path)


def test_smtp(recipient: str | None = None) -> bool:
    host = os.getenv("SMTP_HOST", "smtp.gmail.com").strip()
    port_raw = os.getenv("SMTP_PORT", "587").strip()
    port = int(port_raw) if port_raw.isdigit() else 587
    username = os.getenv("SMTP_USERNAME", "").strip()
    password = os.getenv("SMTP_PASSWORD", "").strip()
    if "gmail" in host.lower():
        password = password.replace(" ", "")
    from_email = os.getenv("SMTP_FROM_EMAIL", username or "no-reply@vaanishield.bank").strip()
    use_tls = os.getenv("SMTP_USE_TLS", "true").strip().lower() in ("true", "1", "yes")
    use_ssl = os.getenv("SMTP_USE_SSL", "false").strip().lower() in ("true", "1", "yes")

    print("=" * 65)
    print("           VaaniShield SMTP Configuration Diagnostic")
    print("=" * 65)
    print(f"Host:       {host}")
    print(f"Port:       {port}")
    print(f"Username:   {username if username else '(NOT CONFIGURED - BLANK)'}")
    print(f"Password:   {'*' * len(password) if password else '(NOT CONFIGURED - BLANK)'}")
    print(f"From:       {from_email}")
    print(f"TLS:        {use_tls}")
    print(f"SSL:        {use_ssl}")
    print("-" * 65)

    if not username or not password:
        print("\n❌ CONFIGURATION ERROR: SMTP_USERNAME or SMTP_PASSWORD is blank in .env")
        if "gmail" in host.lower():
            print("\nHow to configure Gmail SMTP:")
            print(" 1. Go to your Google Account -> Security (https://myaccount.google.com/security)")
            print(" 2. Ensure '2-Step Verification' is turned ON.")
            print(" 3. Navigate to App Passwords (https://myaccount.google.com/apppasswords)")
            print(" 4. Create an app named 'VaaniShield' and copy the 16-character password.")
            print(" 5. In your .env file, update:")
            print("       SMTP_HOST=smtp.gmail.com")
            print("       SMTP_PORT=587")
            print("       SMTP_USERNAME=your_actual_email@gmail.com")
            print("       SMTP_PASSWORD=your_16_char_app_password")
            print("       SMTP_FROM_EMAIL=your_actual_email@gmail.com")
            print("       SMTP_USE_TLS=true")
        return False

    print("\n[1/3] Resolving host and checking network connectivity...")
    try:
        ip = socket.gethostbyname(host)
        print(f"      Resolved {host} -> {ip}")
    except Exception as exc:
        print(f"❌ Failed to resolve host {host}: {exc}")
        return False

    print("[2/3] Connecting and establishing TLS/SSL handshake...")
    try:
        ssl_ctx = ssl.create_default_context()
        if port == 465 or use_ssl:
            server = smtplib.SMTP_SSL(host, port, context=ssl_ctx, timeout=12)
        else:
            server = smtplib.SMTP(host, port, timeout=12)
            if use_tls:
                server.starttls(context=ssl_ctx)
        print("      Connected successfully.")
    except Exception as exc:
        print(f"❌ Connection/Handshake failed: {exc}")
        return False

    print(f"[3/3] Authenticating as '{username}'...")
    try:
        server.login(username, password)
        print(f"      Authentication successful!")
    except smtplib.SMTPAuthenticationError as exc:
        print(f"❌ Authentication failed: {exc}")
        if "gmail" in host.lower():
            print("\nNote: Gmail requires a 16-character Google App Password (not your primary password).")
            print("Visit https://myaccount.google.com/apppasswords to create one.")
        try:
            server.close()
        except Exception:
            pass
        return False
    except Exception as exc:
        print(f"❌ Login failed: {exc}")
        try:
            server.close()
        except Exception:
            pass
        return False

    if recipient:
        print(f"\n[+] Sending test email to '{recipient}'...")
        try:
            from email.mime.text import MIMEText
            msg = MIMEText(
                "Hello,\n\nThis is a test email verifying that your VaaniShield SMTP configuration is working perfectly!\n\nBest regards,\nVaaniShield Security",
                "plain",
                "utf-8",
            )
            msg["Subject"] = "VaaniShield SMTP Test Verification"
            msg["From"] = f"VaaniShield Security <{username}>"
            msg["To"] = recipient
            server.send_message(msg)
            print(f"      Test email dispatched successfully to {recipient}!")
        except Exception as exc:
            print(f"❌ Failed to dispatch email: {exc}")
            try:
                server.close()
            except Exception:
                pass
            return False

    try:
        server.quit()
    except Exception:
        pass

    print("\n✅ SMTP is fully verified and functional!")
    return True


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else None
    ok = test_smtp(target)
    sys.exit(0 if ok else 1)

