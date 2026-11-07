"""Offline voice scam tests with mocked transcription and disposable storage/DB."""

import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient

from backend import auth, main
from services import database, gnani_speech, speech_storage
from services.principal import CustomerPrincipal
from services.speech_adapters import GnaniPrismaTranscriber
from services.speech_interfaces import AudioUploadUnavailable, InputProcessingUnavailable, Transcription


_SECRET = "phase3-scam-test-secret-value-32-bytes-minimum"
_WAV = b"RIFF" + b"\x24\x00\x00\x00" + b"WAVE" + b"fmt " + b"\x00" * 20


def _principal(key="demo-arjun"):
    now = datetime.now(timezone.utc)
    return CustomerPrincipal(key, uuid4(), now - timedelta(minutes=1), now + timedelta(minutes=30))


class _MockTranscriber:
    def __init__(self, text, languages=(), is_code_mixed=False, error=None):
        self.text = text
        self.languages = languages
        self.is_code_mixed = is_code_mixed
        self.error = error
        self.path_seen = None

    def transcribe(self, principal, audio_upload_id, language_hint):
        self.assert_hint = language_hint
        with speech_storage.consume_audio_upload(principal, audio_upload_id) as audio_path:
            self.path_seen = Path(audio_path)
            if self.error:
                raise self.error
            return Transcription(self.text, tuple(self.languages), self.is_code_mixed)


class ScamVoiceAssessmentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.db_patch = patch.object(database, "DB_PATH", root / "voice-scam.sqlite3")
        self.upload_patch = patch.object(speech_storage, "UPLOAD_DIR", root / "uploads")
        self.secret_patch = patch.dict(os.environ, {"VAANISHIELD_DEMO_SESSION_SECRET": _SECRET})
        self.db_patch.start()
        self.upload_patch.start()
        self.secret_patch.start()
        database.initialize_database()
        conn = database.get_connection()
        try:
            conn.executemany(
                "INSERT INTO customers (customer_key, email, name) VALUES (?, ?, ?)",
                [("demo-arjun", "arjun@example.test", "Arjun Demo"),
                 ("demo-priya", "priya@example.test", "Priya Demo")],
            )
            conn.commit()
        finally:
            conn.close()
        self.old_overrides = main.app.dependency_overrides.copy()
        main.app.dependency_overrides.pop(auth.get_authenticated_principal, None)
        self.client = TestClient(main.app)
        self._login("demo-arjun")

    def tearDown(self):
        self.client.close()
        main.app.dependency_overrides.clear()
        main.app.dependency_overrides.update(self.old_overrides)
        self.secret_patch.stop()
        self.upload_patch.stop()
        self.db_patch.stop()
        self.tmp.cleanup()

    def _login(self, key):
        response = self.client.post("/api/v1/auth/demo/session", json={"customer_key": key})
        self.assertEqual(response.status_code, 200, response.text)

    def _upload(self, filename="call.wav", mime="audio/wav", content=_WAV):
        response = self.client.post(
            "/api/v1/voice/uploads",
            files={"file": (filename, content, mime)},
        )
        return response

    def _assess_with(self, transcriber, upload_id, language_hint="auto"):
        with patch("services.speech_adapters.create_speech_transcriber", return_value=transcriber):
            return self.client.post(
                "/api/v1/me/scam/assessments/calls",
                json={"audio_upload_id": upload_id, "language_hint": language_hint},
            )

    def test_authenticated_english_voice_assessment_and_transcript_not_persisted(self):
        upload = self._upload()
        self.assertEqual(upload.status_code, 201)
        mock = _MockTranscriber(
            "Bank support says share your OTP 123456 now.", ("en-IN",), False
        )
        result = self._assess_with(mock, upload.json()["audio_upload_id"], "en-IN")
        self.assertEqual(result.status_code, 201, result.text)
        body = result.json()
        self.assertEqual(body["source_type"], "voice_call")
        self.assertEqual(body["transcript"], mock.text)
        self.assertEqual(body["detected_languages"], ["en-IN"])
        self.assertEqual(body["transcription_status"], "completed")
        self.assertIn("Automated transcription may contain errors", body["transcription_warning"])
        self.assertFalse(mock.path_seen.exists())

        conn = database.get_connection()
        try:
            stored = conn.execute(
                "SELECT * FROM scam_assessments WHERE assessment_id = ?",
                (body["assessment_id"],),
            ).fetchone()
            self.assertEqual(stored["source_mode"], "voice_call")
            self.assertNotIn("transcript", stored.keys())
            self.assertNotIn(mock.text, str(dict(stored)))
        finally:
            conn.close()

    def test_kannada_hindi_and_code_mixed_language_metadata_reach_assessment(self):
        cases = (
            ("à²¨à²¨à³à²¨ OTP à²¹à³‡à²³à²¿", ("kn-IN",), False, ["kn-IN"]),
            ("à¤…à¤ªà¤¨à¤¾ OTP à¤¬à¤¤à¤¾à¤à¤‚", ("hi-IN",), False, ["hi-IN"]),
            ("Share your OTP, à²¨à²¨à³à²¨ UPI PIN", ("en-IN", "kn-IN"), True, ["en-IN", "kn-IN"]),
        )
        for transcript, languages, mixed, expected in cases:
            with self.subTest(languages=languages):
                upload = self._upload()
                mock = _MockTranscriber(transcript, languages, mixed)
                result = self._assess_with(mock, upload.json()["audio_upload_id"])
                self.assertEqual(result.status_code, 201, result.text)
                self.assertEqual(result.json()["detected_languages"], expected)
                self.assertEqual(result.json()["is_code_mixed"], mixed)
                self.assertEqual(result.json()["transcript"], transcript)

    def test_unauthenticated_assessment_is_rejected(self):
        self.client.cookies.clear()
        response = self.client.post(
            "/api/v1/me/scam/assessments/calls",
            json={"audio_upload_id": str(uuid4())},
        )
        self.assertEqual(response.status_code, 401)

    def test_transcription_failure_and_empty_transcript_do_not_create_assessments(self):
        upload = self._upload()
        failed = self._assess_with(
            _MockTranscriber("", error=InputProcessingUnavailable("safe", code="SPEECH_TIMEOUT")),
            upload.json()["audio_upload_id"],
        )
        self.assertEqual(failed.status_code, 503)
        self.assertNotIn("transcript", failed.json())

        upload = self._upload()
        empty = self._assess_with(_MockTranscriber("   "), upload.json()["audio_upload_id"])
        self.assertEqual(empty.status_code, 422)
        self.assertEqual(
            self.client.get("/api/v1/me/scam/assessments").json()["items"], []
        )

    def test_unsupported_audio_and_oversized_upload_are_rejected(self):
        unsupported = self._upload("call.txt", "text/plain", b"not audio")
        self.assertEqual(unsupported.status_code, 415)
        oversized = self._upload(
            "large.wav", "audio/wav", _WAV + b"x" * speech_storage.MAX_AUDIO_SIZE
        )
        self.assertEqual(oversized.status_code, 413)

    def test_prisma_unsupported_format_and_provider_failure_consume_and_clean_upload(self):
        webm = self._upload("call.webm", "audio/webm", b"\x1a\x45\xdf\xa3" + b"x" * 12)
        self.assertEqual(webm.status_code, 201)
        with patch.object(gnani_speech, "API_KEY", "mock-key"):
            unsupported = self._assess_with(
                GnaniPrismaTranscriber(), webm.json()["audio_upload_id"]
            )
        self.assertEqual(unsupported.status_code, 415)

        audio = self._upload()
        upload_id = audio.json()["audio_upload_id"]
        stored_path = next(speech_storage.UPLOAD_DIR.glob("*.wav"))
        with patch.object(gnani_speech, "API_KEY", "mock-key"):
            with patch(
                "services.speech_adapters.gnani_speech.transcribe_audio",
                side_effect=TimeoutError("private provider details"),
            ):
                failed = self._assess_with(GnaniPrismaTranscriber(), upload_id)
        self.assertEqual(failed.status_code, 503)
        self.assertFalse(stored_path.exists())
        with patch.object(gnani_speech, "API_KEY", "mock-key"):
            expired = self._assess_with(GnaniPrismaTranscriber(), upload_id)
        self.assertEqual(expired.status_code, 404)

    def test_foreign_customer_cannot_use_audio_upload(self):
        upload = self._upload()
        self._login("demo-priya")
        response = self._assess_with(
            GnaniPrismaTranscriber(), upload.json()["audio_upload_id"]
        )
        self.assertEqual(response.status_code, 404)
        self.assertEqual(self.client.get("/api/v1/me/scam/assessments").json()["items"], [])

    def test_voice_source_migration_preserves_existing_phase2_assessments(self):
        legacy_path = Path(self.tmp.name) / "phase2-schema.sqlite3"
        with patch.object(database, "DB_PATH", legacy_path):
            conn = database.get_connection()
            try:
                conn.execute(
                    """CREATE TABLE scam_assessments (
                        assessment_id TEXT PRIMARY KEY,
                        customer_key TEXT NOT NULL,
                        source_type TEXT NOT NULL CHECK (source_type = 'typed_message'),
                        risk_level TEXT NOT NULL,
                        indicators_json TEXT NOT NULL,
                        explanation TEXT NOT NULL,
                        evidence_strength TEXT NOT NULL,
                        warning TEXT NOT NULL,
                        recommended_steps_json TEXT NOT NULL,
                        detected_languages_json TEXT NOT NULL,
                        is_code_mixed INTEGER NOT NULL,
                        language_status TEXT NOT NULL,
                        engine_version TEXT NOT NULL,
                        created_at TEXT NOT NULL
                    )"""
                )
                conn.execute(
                    """INSERT INTO scam_assessments VALUES (
                        'legacy-id', 'demo-arjun', 'typed_message', 'LOW', '[]',
                        'Existing result', 'NONE', 'No strong pattern found.', '[]',
                        '[]', 0, 'supported', '1.0.0', '2026-01-01T00:00:00+00:00'
                    )"""
                )
                conn.commit()
            finally:
                conn.close()
            database.initialize_database()
            conn = database.get_connection()
            try:
                row = conn.execute(
                    "SELECT assessment_id, explanation, source_mode FROM scam_assessments"
                ).fetchone()
                self.assertEqual(tuple(row), ("legacy-id", "Existing result", "typed_message"))
            finally:
                conn.close()


if __name__ == "__main__":
    unittest.main()
