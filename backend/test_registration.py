"""Tests for user registration, SMTP email OTP verification, and sign-in flow."""

import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import Response
from starlette.requests import Request

from backend.api_models import (
    DemoLoginRequest,
    RegisterSendOtpRequest,
    RegisterVerifyOtpRequest,
)
from backend.auth import (
    SESSION_COOKIE_NAME,
    demo_login,
    get_authenticated_principal,
    register_send_otp,
    register_verify_otp,
)
from services import database
from services.email_service import generate_otp, send_verification_email, verify_email_otp

TEST_SIGNING_SECRET = "local-demo-auth-test-secret-32-bytes-minimum"


def make_request_with_cookie(token: str) -> Request:
    cookie_header = f"{SESSION_COOKIE_NAME}={token}".encode("ascii")
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/api/v1/auth/demo/session",
        "raw_path": b"/api/v1/auth/demo/session",
        "query_string": b"",
        "root_path": "",
        "headers": [(b"cookie", cookie_header), (b"user-agent", b"TestBrowser/1.0")],
        "server": ("testserver", 80),
        "client": ("127.0.0.1", 50000),
    }
    return Request(scope)


def make_mock_request() -> Request:
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/api/v1/auth/demo/register/verify-otp",
        "raw_path": b"/api/v1/auth/demo/register/verify-otp",
        "query_string": b"",
        "root_path": "",
        "headers": [(b"user-agent", b"Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0")],
        "server": ("testserver", 80),
        "client": ("127.0.0.1", 50000),
    }
    return Request(scope)


class RegistrationAndEmailVerificationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp_dir = tempfile.TemporaryDirectory()
        cls.database_patch = patch.object(
            database, "DB_PATH", Path(cls.temp_dir.name) / "reg-tests.sqlite3"
        )
        cls.database_patch.start()
        with contextlib.redirect_stdout(io.StringIO()):
            database.initialize_database()

    @classmethod
    def tearDownClass(cls):
        cls.database_patch.stop()
        cls.temp_dir.cleanup()

    def setUp(self):
        self.secret_env = patch.dict(
            os.environ,
            {
                "VAANISHIELD_DEMO_SESSION_SECRET": TEST_SIGNING_SECRET,
                "SMTP_HOST": "localhost",
                "SMTP_PORT": "1025",
                "SMTP_USERNAME": "test@example.com",
                "SMTP_PASSWORD": "testpassword",
            },
        )
        self.secret_env.start()
        self.addCleanup(self.secret_env.stop)

        self.smtp_patch = patch("smtplib.SMTP")
        self.mock_smtp = self.smtp_patch.start()
        self.addCleanup(self.smtp_patch.stop)

    def test_email_service_generate_and_verify_otp(self):
        email = "testuser@example.com"
        result = send_verification_email(email, "Test User", otp_code="654321")
        self.assertTrue(result["success"])
        self.assertEqual(result["method"], "smtp")

        # Wrong OTP fails
        valid, msg = verify_email_otp(email, "000000")
        self.assertFalse(valid)

        # Correct OTP succeeds
        valid, msg = verify_email_otp(email, "654321")
        self.assertTrue(valid)

        # Re-use fails (single-use OTP)
        valid2, msg2 = verify_email_otp(email, "654321")
        self.assertFalse(valid2)

    def test_missing_gmail_credentials_raises_informative_error(self):
        with patch.dict(os.environ, {"SMTP_HOST": "smtp.gmail.com", "SMTP_USERNAME": "", "SMTP_PASSWORD": ""}):
            with self.assertRaises(RuntimeError) as cm:
                send_verification_email("unconfigured@example.com", "Test")
            self.assertIn("Gmail SMTP credentials are not configured", str(cm.exception))

    def test_full_registration_and_subsequent_login_flow(self):
        import uuid
        unique_email = f"deepak_{uuid.uuid4().hex[:6]}@example.com"
        reg_payload = RegisterSendOtpRequest(
            name="Deepak Sharma",
            email=unique_email,
            phone="9876543210",
            account_type="SAVINGS",
            monthly_income=65000.0,
            password="SecurePassword@2026",
        )
        # Step 1: Send OTP
        send_resp = register_send_otp(reg_payload)
        self.assertEqual(send_resp.status, "otp_sent")
        
        # In normal mode, OTP is retrieved from the database verification store
        conn = database.get_connection()
        otp = conn.execute(
            "SELECT otp_code FROM email_verifications WHERE email = ? ORDER BY verification_id DESC",
            (unique_email,),
        ).fetchone()[0]
        conn.close()
        self.assertEqual(len(otp), 6)

        # Step 2: Verify OTP & complete registration
        verify_payload = RegisterVerifyOtpRequest(
            email=unique_email,
            otp_code=otp,
            name="Deepak Sharma",
            phone="9876543210",
            account_type="SAVINGS",
            monthly_income=65000.0,
            password="SecurePassword@2026",
        )
        response = Response()
        request = make_mock_request()
        session_resp = register_verify_otp(verify_payload, request, response)

        self.assertEqual(session_resp.display_name, "Deepak Sharma")
        self.assertEqual(session_resp.auth_mode, "REGISTERED")
        self.assertTrue(session_resp.customer_key.startswith("user-"))

        # Check that session cookie was set
        self.assertIn("set-cookie", response.headers)
        self.assertIn(SESSION_COOKIE_NAME, response.headers["set-cookie"])

        # Step 3: Now verify that the registered customer can sign in using their registered email and password!
        login_payload = DemoLoginRequest(
            email=unique_email,
            password="SecurePassword@2026",
        )
        login_response = Response()
        login_session = demo_login(login_payload, request, login_response)

        self.assertEqual(login_session.display_name, "Deepak Sharma")
        self.assertEqual(login_session.customer_key, session_resp.customer_key)
        self.assertEqual(login_session.auth_mode, "REGISTERED")

        # Step 4: Wrong password must fail
        wrong_login = DemoLoginRequest(
            email=unique_email,
            password="WrongPassword123",
        )
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as cm:
            demo_login(wrong_login, request, Response())
        self.assertEqual(cm.exception.status_code, 401)


if __name__ == "__main__":
    unittest.main()

