"""Isolated unit and API tests for Phase 2 Android SMS Threat Intelligence.

All tests execute against temporary SQLite files and never touch data/vaanishield.db.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import tempfile
from threading import Barrier
import unittest
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient

from backend import auth, main
from services import database, notification_service, sms_threat_service
from services.principal import CustomerPrincipal


def _make_principal(customer_key: str = "demo-arjun") -> CustomerPrincipal:
    now = datetime.now(timezone.utc)
    return CustomerPrincipal(
        customer_key=customer_key,
        session_id=uuid4(),
        issued_at=now - timedelta(minutes=1),
        expires_at=now + timedelta(minutes=30),
    )


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class SmsThreatIntelligenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.test_db_path = Path(self.tmp.name) / "sms-threat-tests.sqlite3"
        self.db_patch = patch.object(database, "DB_PATH", self.test_db_path)
        self.db_patch.start()
        sms_threat_service.reset_rate_limits()
        database.initialize_database()
        self._seed_customers()

        self.old_overrides = main.app.dependency_overrides.copy()
        self.active_customer_key = "demo-arjun"
        main.app.dependency_overrides[auth.get_authenticated_principal] = (
            lambda: _make_principal(self.active_customer_key)
        )
        self.client = TestClient(main.app)

    def tearDown(self) -> None:
        self.client.close()
        main.app.dependency_overrides.clear()
        main.app.dependency_overrides.update(self.old_overrides)
        sms_threat_service.reset_rate_limits()
        self.db_patch.stop()
        self.tmp.cleanup()

    @staticmethod
    def _seed_customers() -> None:
        conn = database.get_connection()
        try:
            conn.executemany(
                "INSERT INTO customers (customer_key, email, name) VALUES (?, ?, ?)",
                [
                    ("demo-arjun", "arjun@example.test", "Arjun"),
                    ("demo-priya", "priya@example.test", "Priya"),
                    ("demo-rahul", "rahul@example.test", "Rahul"),
                ],
            )
            conn.commit()
        finally:
            conn.close()

    def _pair_device_for_current_customer(
        self,
        device_id: str = "nord3-arjun-001",
        device_name: str = "Arjun OnePlus Nord 3",
    ) -> dict:
        token_resp = self.client.post(
            "/api/v1/me/scam/devices/pair-token",
            json={"device_name": device_name},
        )
        self.assertEqual(token_resp.status_code, 201, token_resp.text)
        pairing_code = token_resp.json()["pairing_code"]

        pair_resp = self.client.post(
            "/api/v1/devices/pair",
            json={
                "pairing_code": pairing_code,
                "device_id": device_id,
                "device_name": device_name,
                "device_model": "OnePlus CPH2491",
                "android_version": "14",
                "app_version": "1.0.0",
            },
        )
        self.assertEqual(pair_resp.status_code, 201, pair_resp.text)
        data = pair_resp.json()
        data["pairing_code"] = pairing_code
        return data

    # =========================================================================
    # 1. PAIRING TESTS
    # =========================================================================

    def test_pairing_valid_flow_and_no_raw_secrets_in_db(self) -> None:
        paired = self._pair_device_for_current_customer()
        raw_code = paired["pairing_code"]
        raw_token = paired["device_token"]

        # Pairing code must be 8 uppercase chars excluding O, 0, I, 1
        self.assertEqual(len(raw_code), 8)
        for forbidden in ("O", "0", "I", "1"):
            self.assertNotIn(forbidden, raw_code)

        # Device token must be a high-entropy token
        self.assertTrue(raw_token.startswith("vsd_"))
        self.assertEqual(len(raw_token), 68)

        # Inspect database directly: raw code and raw token must NEVER be stored
        conn = database.get_connection()
        try:
            row = conn.execute(
                "SELECT * FROM paired_devices WHERE device_id = ?",
                ("nord3-arjun-001",),
            ).fetchone()
            self.assertIsNotNone(row)
            row_dict = dict(row)
            serialized = json.dumps(row_dict)
            self.assertNotIn(raw_code, serialized)
            self.assertNotIn(raw_token, serialized)
            self.assertEqual(
                row_dict["pairing_code_hash"],
                hashlib.sha256(raw_code.encode("utf-8")).hexdigest(),
            )
            self.assertEqual(
                row_dict["token_hash"],
                hashlib.sha256(raw_token.encode("utf-8")).hexdigest(),
            )
            created_at = datetime.fromisoformat(row_dict["created_at"])
            expires_at = datetime.fromisoformat(row_dict["pairing_code_expires_at"])
            self.assertEqual(
                (expires_at - created_at).total_seconds(),
                sms_threat_service.PAIRING_CODE_TTL_SECONDS,
            )
            self.assertEqual(row_dict["customer_key"], "demo-arjun")
            self.assertEqual(row_dict["status"], "ACTIVE")
        finally:
            conn.close()

    def test_pairing_rejects_invalid_expired_and_reused_codes(self) -> None:
        # 1. Invalid pairing code
        invalid_resp = self.client.post(
            "/api/v1/devices/pair",
            json={
                "pairing_code": "ABCDEFGH",
                "device_id": "nord3-invalid",
                "device_name": "Phone",
                "device_model": "Nord 3",
                "android_version": "14",
                "app_version": "1.0.0",
            },
        )
        self.assertEqual(invalid_resp.status_code, 401)

        # 2. Reused pairing code
        paired = self._pair_device_for_current_customer(device_id="nord3-first")
        reused_resp = self.client.post(
            "/api/v1/devices/pair",
            json={
                "pairing_code": paired["pairing_code"],
                "device_id": "nord3-second",
                "device_name": "Second Phone",
                "device_model": "Nord 3",
                "android_version": "14",
                "app_version": "1.0.0",
            },
        )
        self.assertEqual(reused_resp.status_code, 409)

        # 3. Expired pairing code
        sms_threat_service.reset_rate_limits()
        code_resp = self.client.post("/api/v1/me/scam/devices/pair-token")
        self.assertEqual(code_resp.status_code, 201)
        expired_code = code_resp.json()["pairing_code"]
        past_iso = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
        conn = database.get_connection()
        try:
            conn.execute(
                "UPDATE paired_devices SET pairing_code_expires_at = ? WHERE status = 'PENDING'",
                (past_iso,),
            )
            conn.commit()
        finally:
            conn.close()

        expired_resp = self.client.post(
            "/api/v1/devices/pair",
            json={
                "pairing_code": expired_code,
                "device_id": "nord3-expired",
                "device_name": "Expired Phone",
                "device_model": "Nord 3",
                "android_version": "14",
                "app_version": "1.0.0",
            },
        )
        self.assertEqual(expired_resp.status_code, 400)

    def test_pairing_rate_limiting_blocks_brute_force(self) -> None:
        for _ in range(sms_threat_service.PAIR_ATTEMPT_MAX):
            resp = self.client.post(
                "/api/v1/devices/pair",
                json={
                    "pairing_code": "ZZZZZZZZ",
                    "device_id": "nord3-brute",
                    "device_name": "Phone",
                    "device_model": "Nord 3",
                    "android_version": "14",
                    "app_version": "1.0.0",
                },
            )
            self.assertEqual(resp.status_code, 401)

        blocked = self.client.post(
            "/api/v1/devices/pair",
            json={
                "pairing_code": "ZZZZZZZZ",
                "device_id": "nord3-brute",
                "device_name": "Phone",
                "device_model": "Nord 3",
                "android_version": "14",
                "app_version": "1.0.0",
            },
        )
        self.assertEqual(blocked.status_code, 429)

    def test_concurrent_pairing_attempts_are_atomic_single_use(self) -> None:
        code_resp = self.client.post("/api/v1/me/scam/devices/pair-token")
        self.assertEqual(code_resp.status_code, 201)
        pairing_code = code_resp.json()["pairing_code"]
        start = Barrier(2)

        def attempt_pair(idx: int) -> int:
            start.wait(timeout=5)
            try:
                sms_threat_service.pair_device(
                    pairing_code=pairing_code,
                    device_id=f"nord3-concurrent-{idx}",
                    device_name=f"Phone {idx}",
                    device_model="Nord 3",
                    android_version="14",
                    app_version="1.0.0",
                    client_key=f"concurrent-pair-{idx}",
                )
                return 201
            except sms_threat_service.PairingCodeReusedError:
                return 409

        with ThreadPoolExecutor(max_workers=2) as pool:
            statuses = sorted(pool.map(attempt_pair, [1, 2]))

        self.assertEqual(statuses, [201, 409])

    # =========================================================================
    # 2. DEVICE AUTHENTICATION & ISOLATION TESTS
    # =========================================================================

    def test_device_token_authentication_pause_resume_revoke_and_rotation(self) -> None:
        paired = self._pair_device_for_current_customer(device_id="nord3-lifecycle")
        token = paired["device_token"]
        headers = {"Authorization": f"Bearer {token}"}

        # 1. Valid active token works on heartbeat
        hb = self.client.post("/api/v1/devices/heartbeat", headers=headers, json={"app_version": "1.0.1"})
        self.assertEqual(hb.status_code, 200, hb.text)
        self.assertTrue(hb.json()["forwarding_permitted"])

        # 2. Invalid token is rejected with 401
        bad_hb = self.client.post(
            "/api/v1/devices/heartbeat",
            headers={"Authorization": "Bearer vsd_" + "0" * 64},
        )
        self.assertEqual(bad_hb.status_code, 401)

        # 3. Pause device -> cannot submit SMS or heartbeat (403)
        pause_resp = self.client.post(
            "/api/v1/me/scam/devices/nord3-lifecycle/status",
            json={"status": "PAUSED"},
        )
        self.assertEqual(pause_resp.status_code, 200)
        self.assertEqual(pause_resp.json()["status"], "PAUSED")

        paused_ingest = self.client.post(
            "/api/v1/devices/sms/ingest",
            headers=headers,
            json={
                "source_package": "com.google.android.apps.messaging",
                "sender": "AX-AXISBK",
                "sender_type": "dlt_header",
                "message_text": "Account update notification.",
                "message_timestamp": _now_iso(),
                "client_dedup_key": "dedup-paused-1",
            },
        )
        self.assertEqual(paused_ingest.status_code, 403)

        # 4. Resume device -> works again
        resume_resp = self.client.post(
            "/api/v1/me/scam/devices/nord3-lifecycle/status",
            json={"status": "RESUME"},
        )
        self.assertEqual(resume_resp.status_code, 200)
        self.assertEqual(resume_resp.json()["status"], "ACTIVE")

        # 5. Rotate device token -> old token fails (401), new token succeeds
        rotate_resp = self.client.post("/api/v1/me/scam/devices/nord3-lifecycle/rotate-token")
        self.assertEqual(rotate_resp.status_code, 200)
        new_token = rotate_resp.json()["device_token"]
        self.assertNotEqual(new_token, token)

        old_hb = self.client.post("/api/v1/devices/heartbeat", headers=headers)
        self.assertEqual(old_hb.status_code, 401)
        new_headers = {"Authorization": f"Bearer {new_token}"}
        new_hb = self.client.post("/api/v1/devices/heartbeat", headers=new_headers)
        self.assertEqual(new_hb.status_code, 200)

        # 6. Revoke device -> permanently rejected (403) and cannot be resumed (409)
        revoke_resp = self.client.post(
            "/api/v1/me/scam/devices/nord3-lifecycle/status",
            json={"status": "REVOKED"},
        )
        self.assertEqual(revoke_resp.status_code, 200)
        self.assertEqual(revoke_resp.json()["status"], "REVOKED")

        revoked_ingest = self.client.post(
            "/api/v1/devices/sms/ingest",
            headers=new_headers,
            json={
                "source_package": "com.google.android.apps.messaging",
                "sender": "AX-AXISBK",
                "sender_type": "dlt_header",
                "message_text": "Account update notification.",
                "message_timestamp": _now_iso(),
                "client_dedup_key": "dedup-revoked-1",
            },
        )
        self.assertEqual(revoked_ingest.status_code, 403)

        cannot_resume = self.client.post(
            "/api/v1/me/scam/devices/nord3-lifecycle/status",
            json={"status": "ACTIVE"},
        )
        self.assertEqual(cannot_resume.status_code, 409)

    def test_device_token_cannot_access_banking_or_customer_endpoints(self) -> None:
        paired = self._pair_device_for_current_customer(device_id="nord3-scope-check")
        device_headers = {"Authorization": f"Bearer {paired['device_token']}"}

        # Remove test customer override so real auth is evaluated on banking endpoints
        main.app.dependency_overrides.pop(auth.get_authenticated_principal, None)
        protected_urls = [
            "/api/v1/me/accounts",
            "/api/v1/me/balance",
            "/api/v1/me/transactions",
            "/api/v1/me/cards",
            "/api/v1/me/loans",
            "/api/v1/me/notifications",
            "/api/v1/me/scam/devices",
            "/api/v1/me/scam/sms-threats",
            "/api/customers/demo-arjun/balance",
        ]
        for url in protected_urls:
            resp = self.client.get(url, headers=device_headers)
            self.assertEqual(resp.status_code, 401, f"Expected 401 for {url}, got {resp.status_code}")

    def test_customer_device_isolation_and_device_impersonation_prevention(self) -> None:
        # Pair Device A for Arjun
        self.active_customer_key = "demo-arjun"
        arjun_dev = self._pair_device_for_current_customer(device_id="nord3-arjun")

        # Pair Device B for Priya
        self.active_customer_key = "demo-priya"
        priya_dev = self._pair_device_for_current_customer(device_id="nord3-priya")

        # Priya cannot see or manage Arjun's device
        priya_list = self.client.get("/api/v1/me/scam/devices").json()["items"]
        self.assertEqual([d["device_id"] for d in priya_list], ["nord3-priya"])

        cross_manage = self.client.post(
            "/api/v1/me/scam/devices/nord3-arjun/status",
            json={"status": "REVOKED"},
        )
        self.assertEqual(cross_manage.status_code, 404)

        # Device A's token always records events under Device A & Arjun
        ingest_resp = self.client.post(
            "/api/v1/devices/sms/ingest",
            headers={"Authorization": f"Bearer {arjun_dev['device_token']}"},
            json={
                "source_package": "com.google.android.apps.messaging",
                "sender": "AX-BANK",
                "sender_type": "dlt_header",
                "message_text": "Urgent: bank says share OTP 123456 immediately or account blocked.",
                "message_timestamp": _now_iso(),
                "client_dedup_key": "arjun-msg-1",
            },
        )
        self.assertEqual(ingest_resp.status_code, 201)
        self.assertEqual(ingest_resp.json()["device_id"], "nord3-arjun")

        # Priya cannot see Arjun's ingested SMS event
        self.assertEqual(self.client.get("/api/v1/me/scam/sms-threats").json()["items"], [])

    # =========================================================================
    # 3. MULTILINGUAL SMS INGESTION, VALIDATION, REDACTION & DEDUPLICATION
    # =========================================================================

    def test_multilingual_sms_analysis_english_kannada_hindi_hinglish_and_benign(self) -> None:
        paired = self._pair_device_for_current_customer()
        headers = {"Authorization": f"Bearer {paired['device_token']}"}

        samples = [
            (
                "en-scam",
                "URGENT: Your bank account will be blocked today. Share your OTP 482910 immediately with bank support.",
                {"HIGH", "CRITICAL"},
            ),
            (
                "kn-scam",
                "ನಿಮ್ಮ ಬ್ಯಾಂಕ್ ಖಾತೆ ಇಂದು ಬ್ಲಾಕ್ ಆಗುತ್ತದೆ. ತಕ್ಷಣ ನಿಮ್ಮ OTP 918273 ಹಂಚಿಕೊಳ್ಳಿ.",
                {"HIGH", "CRITICAL"},
            ),
            (
                "hi-scam",
                "\u092c\u0948\u0902\u0915 \u0905\u0927\u093f\u0915\u093e\u0930\u0940 \u0915\u0939\u0924\u093e \u0939\u0948 \u0915\u093f \u0906\u092a\u0915\u093e \u0916\u093e\u0924\u093e \u092c\u094d\u0932\u0949\u0915 \u0939\u094b\u0917\u093e, \u0905\u092d\u0940 \u0905\u092a\u0928\u093e \u0913\u091f\u0940\u092a\u0940 \u092c\u0924\u093e\u090f\u0902\u0964",
                {"HIGH", "CRITICAL"},
            ),
            (
                "hinglish-scam",
                "Aapka bank account block ho jayega immediately, turant apna OTP 778899 share karein.",
                {"HIGH", "CRITICAL"},
            ),
            (
                "benign-msg",
                "Thank you for visiting our branch today. Have a pleasant week ahead.",
                {"LOW"},
            ),
        ]

        for key, text, expected_levels in samples:
            resp = self.client.post(
                "/api/v1/devices/sms/ingest",
                headers=headers,
                json={
                    "source_package": "com.google.android.apps.messaging",
                    "sender": "VM-BANKIN",
                    "sender_type": "dlt_header",
                    "message_text": text,
                    "message_timestamp": _now_iso(),
                    "client_dedup_key": f"sample-{key}",
                },
            )
            self.assertEqual(resp.status_code, 201, f"Failed for {key}: {resp.text}")
            self.assertIn(resp.json()["risk_level"], expected_levels, f"Unexpected risk for {key}")
            if key == "hi-scam":
                detail = sms_threat_service.get_sms_threat(
                    _make_principal(), resp.json()["sms_event_id"]
                )
                self.assertIn("hi-IN", detail["detected_languages"])

    def test_sms_ingestion_input_validation_and_spoofing_rejection(self) -> None:
        paired = self._pair_device_for_current_customer()
        headers = {"Authorization": f"Bearer {paired['device_token']}"}
        base_payload = {
            "source_package": "com.google.android.apps.messaging",
            "sender": "AX-AXISBK",
            "sender_type": "dlt_header",
            "message_text": "Valid message body.",
            "message_timestamp": _now_iso(),
            "client_dedup_key": "valid-key-1",
        }

        # 1. Empty message rejected
        r_empty = self.client.post(
            "/api/v1/devices/sms/ingest",
            headers=headers,
            json={**base_payload, "message_text": "   ", "client_dedup_key": "k-empty"},
        )
        self.assertEqual(r_empty.status_code, 422)

        # 2. Excessively long message rejected
        r_long = self.client.post(
            "/api/v1/devices/sms/ingest",
            headers=headers,
            json={**base_payload, "message_text": "A" * 2500, "client_dedup_key": "k-long"},
        )
        self.assertEqual(r_long.status_code, 422)

        # 3. Unsupported source_package rejected
        r_pkg = self.client.post(
            "/api/v1/devices/sms/ingest",
            headers=headers,
            json={**base_payload, "source_package": "com.whatsapp", "client_dedup_key": "k-pkg"},
        )
        self.assertEqual(r_pkg.status_code, 422)

        # 4. Invalid timestamp rejected
        r_ts = self.client.post(
            "/api/v1/devices/sms/ingest",
            headers=headers,
            json={**base_payload, "message_timestamp": "not-a-timestamp", "client_dedup_key": "k-ts"},
        )
        self.assertEqual(r_ts.status_code, 422)

        # 5. Customer identity spoofing in payload rejected (extra="forbid")
        r_spoof = self.client.post(
            "/api/v1/devices/sms/ingest",
            headers=headers,
            json={**base_payload, "customer_key": "demo-priya", "client_dedup_key": "k-spoof"},
        )
        self.assertEqual(r_spoof.status_code, 422)

    def test_sensitive_information_redacted_before_persistence_and_scam_assessments_untouched(self) -> None:
        paired = self._pair_device_for_current_customer()
        headers = {"Authorization": f"Bearer {paired['device_token']}"}

        raw_sms = (
            "Hello Arjun, bank officer says your account 12345678901234 is frozen. "
            "Share OTP for login is 839201 and UPI PIN: 4921 immediately or call 9876543210. "
            "Visit https://evil.example/kyc?token=sec998877"
        )
        resp = self.client.post(
            "/api/v1/devices/sms/ingest",
            headers=headers,
            json={
                "source_package": "com.google.android.apps.messaging",
                "sender": "+919876543210",
                "sender_type": "phone_number",
                "message_text": raw_sms,
                "message_timestamp": _now_iso(),
                "client_dedup_key": "redact-test-1",
            },
        )
        self.assertEqual(resp.status_code, 201, resp.text)
        sms_event_id = resp.json()["sms_event_id"]

        detail = self.client.get(f"/api/v1/me/scam/sms-threats/{sms_event_id}").json()
        serialized_detail = json.dumps(detail)
        for secret_val in ("839201", "4921", "12345678901234", "sec998877"):
            self.assertNotIn(secret_val, serialized_detail)

        conn = database.get_connection()
        try:
            row = conn.execute(
                "SELECT * FROM sms_threat_events WHERE sms_event_id = ?",
                (sms_event_id,),
            ).fetchone()
            db_serialized = json.dumps(dict(row))
            for secret_val in ("839201", "4921", "12345678901234", "sec998877"):
                self.assertNotIn(secret_val, db_serialized)

            # Original scam_assessments table must have 0 rows from Android SMS ingestion
            scam_count = conn.execute("SELECT COUNT(*) FROM scam_assessments").fetchone()[0]
            self.assertEqual(scam_count, 0)
        finally:
            conn.close()

    def test_idempotent_deduplication_and_legitimate_repeated_messages(self) -> None:
        paired = self._pair_device_for_current_customer()
        headers = {"Authorization": f"Bearer {paired['device_token']}"}
        payload = {
            "source_package": "com.google.android.apps.messaging",
            "sender": "AX-AXISBK",
            "sender_type": "dlt_header",
            "message_text": "URGENT: Your account is suspended. Share your OTP 654321 immediately.",
            "message_timestamp": _now_iso(),
            "client_dedup_key": "notif-id-1001",
        }

        first = self.client.post("/api/v1/devices/sms/ingest", headers=headers, json=payload)
        self.assertEqual(first.status_code, 201)
        self.assertFalse(first.json()["deduplicated"])

        # Duplicate submission with same client_dedup_key returns existing event without duplicating
        second = self.client.post("/api/v1/devices/sms/ingest", headers=headers, json=payload)
        self.assertEqual(second.status_code, 200)
        self.assertTrue(second.json()["deduplicated"])
        self.assertEqual(second.json()["sms_event_id"], first.json()["sms_event_id"])

        # Legitimate repeated message with same text but distinct message/notification ID is preserved
        repeated_at = (
            datetime.fromisoformat(payload["message_timestamp"])
            + timedelta(seconds=1)
        ).isoformat()
        third = self.client.post(
            "/api/v1/devices/sms/ingest",
            headers=headers,
            json={
                **payload,
                "message_timestamp": repeated_at,
                "client_dedup_key": "notif-id-1002",
            },
        )
        self.assertEqual(third.status_code, 201)
        self.assertFalse(third.json()["deduplicated"])
        self.assertNotEqual(third.json()["sms_event_id"], first.json()["sms_event_id"])

        # Concurrent duplicate submissions use independent service connections
        # and retain the production BEGIN IMMEDIATE transaction path.
        device_principal = sms_threat_service.authenticate_device_token(
            paired["device_token"]
        )
        concurrent_payload = {
            "source_package": payload["source_package"],
            "sender": payload["sender"],
            "sender_type": payload["sender_type"],
            "message_text": payload["message_text"],
            "message_timestamp": _now_iso(),
            "client_dedup_key": "notif-concurrent-99",
        }
        start = Barrier(3)

        def ingest_concurrently(_: int) -> dict:
            start.wait(timeout=5)
            return sms_threat_service.ingest_sms_event(
                device_principal, **concurrent_payload
            )

        with ThreadPoolExecutor(max_workers=3) as pool:
            results = list(pool.map(ingest_concurrently, range(3)))

        event_ids = {r["sms_event_id"] for r in results}
        self.assertEqual(len(event_ids), 1)
        self.assertEqual(sum(not r["deduplicated"] for r in results), 1)

        conn = database.get_connection()
        try:
            event_count = conn.execute(
                "SELECT COUNT(*) FROM sms_threat_events WHERE dedup_fingerprint = ?",
                (sms_threat_service._sha256_hex(
                    f"demo-arjun|{device_principal.device_id}|notif-concurrent-99"
                ),),
            ).fetchone()[0]
            notification_count = conn.execute(
                "SELECT COUNT(*) FROM notifications WHERE event_ref = ?",
                (f"sms-threat-{next(iter(event_ids))}",),
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(event_count, 1)
        self.assertEqual(notification_count, 1)

    # =========================================================================
    # 4. NOTIFICATION INTEGRATION TESTS
    # =========================================================================

    def test_notifications_generated_only_for_high_and_critical_and_idempotent(self) -> None:
        paired = self._pair_device_for_current_customer()
        headers = {"Authorization": f"Bearer {paired['device_token']}"}

        # 1. LOW risk -> no notification
        low_resp = self.client.post(
            "/api/v1/devices/sms/ingest",
            headers=headers,
            json={
                "source_package": "com.google.android.apps.messaging",
                "sender": "AX-AXISBK",
                "sender_type": "dlt_header",
                "message_text": "Your monthly statement is ready for viewing.",
                "message_timestamp": _now_iso(),
                "client_dedup_key": "notif-low-1",
            },
        ).json()
        self.assertEqual(low_resp["risk_level"], "LOW")
        self.assertFalse(low_resp["alert_generated"])

        # 2. MEDIUM risk -> no notification
        med_resp = self.client.post(
            "/api/v1/devices/sms/ingest",
            headers=headers,
            json={
                "source_package": "com.google.android.apps.messaging",
                "sender": "VK-PRIZE",
                "sender_type": "dlt_header",
                "message_text": "Congratulations! You have won a lottery prize. Act immediately within 10 minutes!",
                "message_timestamp": _now_iso(),
                "client_dedup_key": "notif-med-1",
            },
        ).json()
        self.assertEqual(med_resp["risk_level"], "MEDIUM")
        self.assertFalse(med_resp["alert_generated"])

        # 3. UNKNOWN risk (unsupported script) -> no notification
        unk_resp = self.client.post(
            "/api/v1/devices/sms/ingest",
            headers=headers,
            json={
                "source_package": "com.google.android.apps.messaging",
                "sender": "UNKNOWN",
                "sender_type": "unknown",
                "message_text": "こんにちは世界",
                "message_timestamp": _now_iso(),
                "client_dedup_key": "notif-unk-1",
            },
        ).json()
        self.assertEqual(unk_resp["risk_level"], "UNKNOWN")
        self.assertFalse(unk_resp["alert_generated"])

        # Verify 0 security_alert notifications so far
        notifs_before = notification_service.list_notifications("demo-arjun")
        sms_notifs_before = [
            n for n in notifs_before if str(n.get("event_ref") or "").startswith("sms-threat-")
        ]
        self.assertEqual(len(sms_notifs_before), 0)

        # 4. HIGH risk -> generates notification
        high_resp = self.client.post(
            "/api/v1/devices/sms/ingest",
            headers=headers,
            json={
                "source_package": "com.google.android.apps.messaging",
                "sender": "VM-SCAM",
                "sender_type": "dlt_header",
                "message_text": "Please share your OTP 112233 to complete verification.",
                "message_timestamp": _now_iso(),
                "client_dedup_key": "notif-high-1",
            },
        ).json()
        self.assertEqual(high_resp["risk_level"], "HIGH")
        self.assertTrue(high_resp["alert_generated"])

        # 5. CRITICAL risk (submitted twice to verify notification idempotency)
        crit_payload = {
            "source_package": "com.google.android.apps.messaging",
            "sender": "AD-BANK",
            "sender_type": "dlt_header",
            "message_text": "URGENT: Calling from RBI bank support. Your account will be blocked unless you share OTP 998877 immediately.",
            "message_timestamp": _now_iso(),
            "client_dedup_key": "notif-crit-1",
        }
        crit_resp = self.client.post("/api/v1/devices/sms/ingest", headers=headers, json=crit_payload).json()
        self.assertEqual(crit_resp["risk_level"], "CRITICAL")
        self.assertTrue(crit_resp["alert_generated"])

        # Duplicate submission
        self.client.post("/api/v1/devices/sms/ingest", headers=headers, json=crit_payload)

        notifs_after = notification_service.list_notifications("demo-arjun")
        sms_notifs_after = [
            n for n in notifs_after if str(n.get("event_ref") or "").startswith("sms-threat-")
        ]
        self.assertEqual(len(sms_notifs_after), 2)
        self.assertTrue(all(n["category"] == "security_alert" for n in sms_notifs_after))
        self.assertGreaterEqual(notification_service.get_unread_count("demo-arjun"), 2)

    # =========================================================================
    # 5. CUSTOMER SMS HISTORY, FILTERS, PAGINATION & ACKNOWLEDGMENT TESTS
    # =========================================================================

    def test_customer_sms_history_pagination_filters_and_acknowledgment(self) -> None:
        paired = self._pair_device_for_current_customer(device_id="nord3-history")
        headers = {"Authorization": f"Bearer {paired['device_token']}"}

        # Ingest 2 LOW and 1 CRITICAL event for Arjun
        for idx in range(2):
            self.client.post(
                "/api/v1/devices/sms/ingest",
                headers=headers,
                json={
                    "source_package": "com.google.android.apps.messaging",
                    "sender": "AX-INFO",
                    "sender_type": "dlt_header",
                    "message_text": f"Informational bank update #{idx}.",
                    "message_timestamp": _now_iso(),
                    "client_dedup_key": f"hist-low-{idx}",
                },
            )

        crit_event = self.client.post(
            "/api/v1/devices/sms/ingest",
            headers=headers,
            json={
                "source_package": "com.google.android.apps.messaging",
                "sender": "AX-ALERT",
                "sender_type": "dlt_header",
                "message_text": "Calling from bank support: Your account is suspended immediately. Share OTP 334455 now.",
                "message_timestamp": _now_iso(),
                "client_dedup_key": "hist-crit-1",
            },
        ).json()
        crit_id = crit_event["sms_event_id"]

        # Pagination test
        page1 = self.client.get("/api/v1/me/scam/sms-threats?limit=2").json()
        self.assertEqual(len(page1["items"]), 2)
        self.assertTrue(page1["has_more"])
        self.assertEqual(page1["items"][0]["sms_event_id"], crit_id)

        page2 = self.client.get(
            "/api/v1/me/scam/sms-threats",
            params={"limit": 2, "cursor": page1["next_cursor"]},
        ).json()
        self.assertEqual(len(page2["items"]), 1)
        self.assertFalse(page2["has_more"])

        # Filter by risk_level
        crit_only = self.client.get(
            "/api/v1/me/scam/sms-threats",
            params={"risk_level": "CRITICAL"},
        ).json()["items"]
        self.assertEqual(len(crit_only), 1)
        self.assertEqual(crit_only[0]["sms_event_id"], crit_id)

        # Acknowledge CRITICAL event (idempotent)
        ack1 = self.client.post(f"/api/v1/me/scam/sms-threats/{crit_id}/acknowledge")
        self.assertEqual(ack1.status_code, 200)
        self.assertTrue(ack1.json()["acknowledged"])
        first_ack_time = ack1.json()["acknowledged_at"]
        self.assertIsNotNone(first_ack_time)

        ack2 = self.client.post(f"/api/v1/me/scam/sms-threats/{crit_id}/acknowledge")
        self.assertEqual(ack2.status_code, 200)
        self.assertEqual(ack2.json()["acknowledged_at"], first_ack_time)

        # Filter by acknowledged=true vs false
        ack_items = self.client.get(
            "/api/v1/me/scam/sms-threats", params={"acknowledged": "true"}
        ).json()["items"]
        self.assertEqual(len(ack_items), 1)
        unack_items = self.client.get(
            "/api/v1/me/scam/sms-threats", params={"acknowledged": "false"}
        ).json()["items"]
        self.assertEqual(len(unack_items), 2)

        # Switch to Priya: cannot view or acknowledge Arjun's event
        self.active_customer_key = "demo-priya"
        self.assertEqual(
            self.client.get(f"/api/v1/me/scam/sms-threats/{crit_id}").status_code,
            404,
        )
        self.assertEqual(
            self.client.post(f"/api/v1/me/scam/sms-threats/{crit_id}/acknowledge").status_code,
            404,
        )


if __name__ == "__main__":
    unittest.main()

