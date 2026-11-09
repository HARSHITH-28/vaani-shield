"""Isolated tests for the authenticated unified scam reports API."""

from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient

from backend import auth, main
from services import database, scam_assessment_service
from services.principal import CustomerPrincipal
from services.scam_report_service import get_report


class ScamReportsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(database, "DB_PATH", Path(self.tmp.name) / "reports.sqlite3")
        self.db_patch.start()
        database.initialize_database()
        conn = database.get_connection()
        conn.executemany(
            "INSERT INTO customers (customer_key, email, name) VALUES (?, ?, ?)",
            [("demo-arjun", "arjun@test", "Arjun"), ("demo-priya", "priya@test", "Priya")],
        )
        conn.commit()
        conn.close()
        now = datetime.now(timezone.utc)
        self.principal = CustomerPrincipal("demo-arjun", uuid4(), now - timedelta(minutes=1), now + timedelta(minutes=30))
        self.old_overrides = main.app.dependency_overrides.copy()
        main.app.dependency_overrides[auth.get_authenticated_principal] = lambda: self.principal
        self.client = TestClient(main.app)

    def tearDown(self):
        self.client.close()
        main.app.dependency_overrides.clear()
        main.app.dependency_overrides.update(self.old_overrides)
        self.db_patch.stop()
        self.tmp.cleanup()

    def _sms(self, risk, event_id=None, owner="demo-arjun", received=None):
        event_id = str(event_id or uuid4())
        received = received or datetime.now(timezone.utc).isoformat()
        conn = database.get_connection()
        conn.execute(
            "INSERT OR IGNORE INTO paired_devices (device_id, customer_key, device_name, status, created_at, updated_at) "
            "VALUES ('test-device', ?, 'Test', 'ACTIVE', ?, ?)", (owner, received, received)
        )
        conn.execute(
            """INSERT INTO sms_threat_events (
                sms_event_id, customer_key, device_id, dedup_fingerprint, source_package, sender,
                sender_type, redacted_message, message_timestamp, received_at, risk_level,
                evidence_strength, indicators_json, explanation, warning, recommendations_json,
                detected_languages_json, code_mixed, language_status, engine_version,
                alert_generated, acknowledged
            ) VALUES (?, ?, 'test-device', ?, 'com.test.sms', 'TEST', 'unknown', '[NUMBER REDACTED] offer',
                ?, ?, ?, 'LIMITED', '[]', 'persisted search needle', 'warning', '[]', '[]', 0,
                'uncertain', 'test-engine', 0, 0)""",
            (event_id, owner, str(uuid4()), received, received, risk),
        )
        conn.commit()
        conn.close()
        return event_id

    def test_all_sms_risks_and_customer_scoped_detail(self):
        ids = {risk: self._sms(risk) for risk in ("UNKNOWN", "LOW", "MEDIUM", "HIGH", "CRITICAL")}
        page = self.client.get("/api/v1/me/scam/reports?source_type=INCOMING_SMS&limit=100")
        self.assertEqual(page.status_code, 200, page.text)
        self.assertEqual({r["risk_level"] for r in page.json()["items"]}, set(ids))
        for risk, report_id in ids.items():
            response = self.client.get(f"/api/v1/me/scam/reports/INCOMING_SMS/{report_id}")
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["risk_level"], risk)
        self._sms("HIGH", owner="demo-priya")
        self.assertEqual(self.client.get(f"/api/v1/me/scam/reports/INCOMING_SMS/{ids['HIGH']}").status_code, 200)
        conn = database.get_connection()
        foreign_id = conn.execute("SELECT sms_event_id FROM sms_threat_events WHERE customer_key='demo-priya'").fetchone()[0]
        conn.close()
        self.assertEqual(self.client.get(f"/api/v1/me/scam/reports/INCOMING_SMS/{foreign_id}").status_code, 404)

    def test_filters_search_keyset_and_no_reanalysis_on_read(self):
        self._sms("HIGH")
        created = self.client.post("/api/v1/me/scam/assessments/messages", json={"message_text": "persisted search needle", "language_hint": "en-IN"})
        self.assertEqual(created.status_code, 201, created.text)
        manual_id = created.json()["assessment_id"]
        risk = created.json()["risk_level"]
        with patch("services.scam_risk_engine.analyze_scam_text", side_effect=AssertionError("reanalysis")):
            filtered = self.client.get(f"/api/v1/me/scam/reports?source_type=MANUAL_MESSAGE&risk_level={risk}&search=source_content_not_stored")
            self.assertEqual(filtered.status_code, 200, filtered.text)
            self.assertEqual([x["report_id"] for x in filtered.json()["items"]], [manual_id])
            self.assertEqual(self.client.get(f"/api/v1/me/scam/reports/MANUAL_MESSAGE/{manual_id}").status_code, 200)
        unavailable_search = self.client.get("/api/v1/me/scam/reports?source_type=MANUAL_MESSAGE&search=needle")
        self.assertEqual(unavailable_search.json()["items"], [])
        start = (datetime.now(timezone.utc) - timedelta(days=1)).date().isoformat()
        end = (datetime.now(timezone.utc) + timedelta(days=1)).date().isoformat()
        self.assertEqual(self.client.get(f"/api/v1/me/scam/reports?from={start}&to={end}").status_code, 200)
        first = self.client.get("/api/v1/me/scam/reports?limit=1").json()
        self.assertTrue(first["has_more"])
        # A new row before the cursor does not change which older row follows it.
        self._sms("LOW")
        second = self.client.get("/api/v1/me/scam/reports?limit=1&cursor=" + first["next_cursor"]).json()
        self.assertNotEqual(first["items"][0]["report_id"], second["items"][0]["report_id"])
        self.assertEqual(self.client.get("/api/v1/me/scam/reports?risk_level=bogus").status_code, 400)

    def test_expiry_clears_only_transcript_and_historical_call_stays(self):
        call = scam_assessment_service.create_assessment(
            self.principal, "Bank support OTP 123456", "auto", _source_mode="voice_call",
            _redacted_transcript="Bank support OTP [REDACTED]",
        )
        conn = database.get_connection()
        conn.execute("UPDATE scam_assessments SET transcript_expires_at=? WHERE assessment_id=?",
                     ((datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat(), call["assessment_id"]))
        conn.commit()
        conn.close()
        before_cleanup = get_report(self.principal, "VOICE_CALL", call["assessment_id"])
        self.assertIsNone(before_cleanup["redacted_call_transcript"])
        conn = database.get_connection()
        retained_until_cleanup = conn.execute(
            "SELECT redacted_transcript FROM scam_assessments WHERE assessment_id=?",
            (call["assessment_id"],),
        ).fetchone()[0]
        conn.close()
        self.assertEqual(retained_until_cleanup, "Bank support OTP [REDACTED]")
        detail = self.client.get(f"/api/v1/me/scam/reports/VOICE_CALL/{call['assessment_id']}")
        self.assertEqual(detail.status_code, 200, detail.text)
        self.assertIsNone(detail.json()["redacted_call_transcript"])
        self.assertEqual(detail.json()["content_availability"], "TRANSCRIPT_EXPIRED")
        conn = database.get_connection()
        row = conn.execute("SELECT redacted_transcript, transcript_expires_at, transcript_status, explanation FROM scam_assessments WHERE assessment_id=?", (call["assessment_id"],)).fetchone()
        conn.close()
        self.assertIsNone(row["redacted_transcript"])
        self.assertIsNone(row["transcript_expires_at"])
        self.assertEqual(row["transcript_status"], "EXPIRED")
        self.assertEqual(detail.json()["explanation"], row["explanation"])


if __name__ == "__main__":
    unittest.main()
