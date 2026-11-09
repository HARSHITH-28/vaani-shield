"""Local-only checks for the demo authentication foundation."""

import contextlib
import io
import os
import tempfile
import unittest
from datetime import datetime, timezone
from http.cookies import SimpleCookie
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from fastapi import HTTPException
from fastapi import Response
from pydantic import ValidationError
from starlette.requests import Request

from backend.api_models import AssistantTurnRequest, DemoSessionRequest
from backend.auth import (
    SESSION_COOKIE_NAME,
    SESSION_COOKIE_PATH,
    SESSION_TTL_SECONDS,
    _encode_session_token,
    create_demo_session,
    delete_demo_session,
    get_authenticated_principal,
    read_demo_session,
)
from services.agent_tools import execute_tool
from services import database


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
        "headers": [(b"cookie", cookie_header)],
        "server": ("testserver", 80),
        "client": ("testclient", 50000),
    }
    return Request(scope)


class DemoAuthenticationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp_dir = tempfile.TemporaryDirectory()
        cls.supabase_patch = patch.object(
            database, "is_supabase_configured", return_value=False
        )
        cls.supabase_patch.start()
        cls.database_patch = patch.object(
            database, "DB_PATH", Path(cls.temp_dir.name) / "auth-tests.sqlite3"
        )
        cls.database_patch.start()
        with contextlib.redirect_stdout(io.StringIO()):
            database.initialize_database()
        conn = database.get_connection()
        try:
            conn.executemany(
                "INSERT INTO customers (customer_key, email, name) VALUES (?, ?, ?)",
                [
                    ("demo-arjun", "arjun@example.test", "Arjun Demo"),
                    ("demo-priya", "priya@example.test", "Priya Demo"),
                    ("demo-rahul", "rahul@example.test", "Rahul Demo"),
                ],
            )
            conn.commit()
        finally:
            conn.close()

    @classmethod
    def tearDownClass(cls):
        cls.database_patch.stop()
        cls.supabase_patch.stop()
        cls.temp_dir.cleanup()

    def setUp(self):
        self.secret_env = patch.dict(
            os.environ,
            {"VAANISHIELD_DEMO_SESSION_SECRET": TEST_SIGNING_SECRET},
        )
        self.secret_env.start()
        self.addCleanup(self.secret_env.stop)

    def create_token(self, customer_key: str) -> tuple[str, Response]:
        response = Response()
        session_body = create_demo_session(
            DemoSessionRequest(customer_key=customer_key),
            response,
        )
        self.assertNotIn("access_token", session_body.model_dump())
        self.assertNotIn("session_token", session_body.model_dump())
        for header_name, header_value in response.raw_headers:
            if header_name.lower() != b"set-cookie":
                continue
            header = header_value.decode("latin-1")
            cookie = SimpleCookie()
            cookie.load(header)
            morsel = cookie.get(SESSION_COOKIE_NAME)
            if morsel and morsel.value and morsel["path"] == SESSION_COOKIE_PATH:
                return morsel.value, response
        self.fail("Demo-session response did not issue the active session cookie.")

    def test_valid_session_uses_http_only_signed_cookie(self):
        token, response = self.create_token("demo-arjun")
        principal = get_authenticated_principal(make_request_with_cookie(token))

        self.assertEqual(principal.customer_key, "demo-arjun")
        self.assertEqual(principal.auth_method, "demo_cookie")
        session_info = read_demo_session(Response(), principal)
        self.assertEqual(session_info.customer_key, "demo-arjun")
        self.assertIn("httponly", response.headers["set-cookie"].lower())
        self.assertIn(f"path={SESSION_COOKIE_PATH}", response.headers["set-cookie"].lower())

    def test_expired_session_is_rejected(self):
        now = int(datetime.now(timezone.utc).timestamp())
        token = _encode_session_token(
            "demo-arjun",
            now - SESSION_TTL_SECONDS - 60,
            now - 60,
            uuid4(),
        )

        with self.assertRaises(HTTPException) as raised:
            get_authenticated_principal(make_request_with_cookie(token))
        self.assertEqual(raised.exception.status_code, 401)

    def test_invalid_session_is_rejected_without_token_details(self):
        with self.assertRaises(HTTPException) as raised:
            get_authenticated_principal(make_request_with_cookie("invalid.token.value"))
        self.assertEqual(raised.exception.status_code, 401)
        self.assertNotIn("invalid.token.value", str(raised.exception.detail))

    def test_missing_session_is_rejected(self):
        request = Request({
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/api/v1/auth/demo/session",
            "raw_path": b"/api/v1/auth/demo/session",
            "query_string": b"",
            "root_path": "",
            "headers": [],
            "server": ("testserver", 80),
            "client": ("testclient", 50000),
        })
        with self.assertRaises(HTTPException) as raised:
            get_authenticated_principal(request)
        self.assertEqual(raised.exception.status_code, 401)

    def test_logout_clears_session_cookie(self):
        response = Response()
        body = delete_demo_session(response)
        self.assertEqual(body.status, "signed_out")
        self.assertIn("max-age=0", response.headers["set-cookie"].lower())
        self.assertIn(f"path={SESSION_COOKIE_PATH}", response.headers["set-cookie"].lower())

    def test_principal_isolation_and_tool_identity_cannot_be_overridden(self):
        arjun_token, _ = self.create_token("demo-arjun")
        priya_token, _ = self.create_token("demo-priya")
        arjun = get_authenticated_principal(make_request_with_cookie(arjun_token))
        priya = get_authenticated_principal(make_request_with_cookie(priya_token))

        arjun_result = execute_tool("get_customer_profile", arjun, {})
        priya_result = execute_tool("get_customer_profile", priya, {})
        self.assertNotEqual(arjun_result["name"], priya_result["name"])
        for result in (arjun_result, priya_result):
            self.assertNotIn("customer_id", result)
            self.assertNotIn("customer_key", result)

        with self.assertRaises(ValueError):
            execute_tool("get_customer_profile", 4, {})
        with self.assertRaises(ValueError):
            execute_tool("get_account_balance", arjun, {"customer_key": "demo-priya"})

    def test_assistant_request_cannot_choose_customer_identity(self):
        for field in ("customer_id", "customer_key"):
            payload = {
                "client_turn_id": uuid4(),
                "input_mode": "text",
                "output_mode": "text",
                "text": "Show my balance",
                field: "demo-priya" if field == "customer_key" else 5,
            }
            with self.subTest(field=field), self.assertRaises(ValidationError):
                AssistantTurnRequest(**payload)

        with self.assertRaises(ValidationError):
            DemoSessionRequest(customer_key="arbitrary-customer")


if __name__ == "__main__":
    unittest.main()
