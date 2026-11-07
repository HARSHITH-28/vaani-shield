"""Mocked Prisma/Timbre integration tests; no provider requests are made."""

import tempfile
import unittest
import os
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from uuid import UUID, uuid4

from fastapi import HTTPException, UploadFile
from fastapi.responses import FileResponse
from starlette.datastructures import Headers
from starlette.requests import Request

from backend.api_models import AssistantTurnRequest
from backend.assistant_voice_routes import retrieve_assistant_audio, upload_assistant_audio
from services import database, gnani_speech, speech_storage
from services.assistant_service import (
    AssistantOrchestrator,
    InputProcessingUnavailable,
    OutputProcessingUnavailable,
)
from services.banking_service import get_customer_profile_by_key
from services.conversation_service import get_conversation_history
from services.principal import CustomerPrincipal
from services.speech_adapters import (
    GnaniPrismaTranscriber,
    GnaniTimbreSynthesizer,
    GroqWhisperTranscriber,
)


def _principal(key="demo-arjun"):
    now = datetime.now(timezone.utc)
    return CustomerPrincipal(key, uuid4(), now - timedelta(minutes=1), now + timedelta(minutes=20))


def _wav_bytes():
    return b"RIFF" + b"\x24\x00\x00\x00" + b"WAVE" + b"fmt " + b"\x00" * 20


def _request(input_mode="text", output_mode="text", text="What is my balance?", audio_id=None):
    fields = {
        "client_turn_id": uuid4(),
        "input_mode": input_mode,
        "output_mode": output_mode,
    }
    if input_mode == "text":
        fields["text"] = text
    else:
        fields["audio_upload_id"] = audio_id or uuid4()
        fields["language_hint"] = "auto"
    return AssistantTurnRequest(**fields)


class SpeechIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.db_patch = patch.object(database, "DB_PATH", root / "test.sqlite3")
        self.upload_patch = patch.object(speech_storage, "UPLOAD_DIR", root / "uploads")
        self.audio_patch = patch("services.speech_adapters.gnani_speech.AUDIO_DIR", root / "generated")
        self.provider_patch = patch.dict(os.environ, {"VAANISHIELD_LLM_PROVIDER": "rule_based"})
        self.db_patch.start()
        self.upload_patch.start()
        self.audio_patch.start()
        self.provider_patch.start()
        database.initialize_database()
        conn = database.get_connection()
        try:
            for key, email, name in (
                ("demo-arjun", "arjun@example.com", "Arjun Demo"),
                ("demo-priya", "priya@example.com", "Priya Demo"),
            ):
                conn.execute(
                    "INSERT INTO customers (customer_key, email, name) VALUES (?, ?, ?)",
                    (key, email, name),
                )
            conn.commit()
        finally:
            conn.close()
        self.arjun = _principal()
        self.priya = _principal("demo-priya")
        self.orchestrator = AssistantOrchestrator(
            transcriber=GnaniPrismaTranscriber(),
            synthesizer=GnaniTimbreSynthesizer(),
        )
        self.artifact_ids = []

    def tearDown(self):
        for artifact_id in self.artifact_ids:
            speech_storage.delete_speech_artifact(artifact_id)
        self.audio_patch.stop()
        self.upload_patch.stop()
        self.provider_patch.stop()
        self.db_patch.stop()
        self.temp_dir.cleanup()

    def _store_upload(self, principal=None, content=None, filename="sample.wav", mime="audio/wav"):
        return speech_storage.store_audio_upload(
            principal or self.arjun, filename, mime, content or _wav_bytes()
        )

    def _mock_timbre(self, text_seen, language_seen, voice_seen):
        def synthesize(text, language, voice, filename):
            text_seen.append(text)
            language_seen.append(language)
            voice_seen.append(voice)
            directory = Path(gnani_speech.AUDIO_DIR)
            directory.mkdir(parents=True, exist_ok=True)
            output = directory / filename
            output.write_bytes(_wav_bytes())
            return {"audio_path": str(output)}
        return synthesize

    def test_speech_input_transcribes_and_persists_transcript_only(self):
        upload = self._store_upload()
        observed_paths = []

        def prisma(path, language_code, **options):
            observed_paths.append(Path(path))
            self.assertTrue(Path(path).exists())
            self.assertEqual(options["output_mode"], "transcribe")
            self.assertIn("UPI", options["keyterms"])
            return {
                "transcript": "ನನ್ನ balance",
                "raw_response": {"detected_languages": ["kn-IN", "en-IN"], "is_code_mixed": True},
            }

        with patch("services.speech_adapters.gnani_speech.transcribe_audio", side_effect=prisma):
            with patch("services.assistant_service.execute_tool", return_value=[]):
                response = self.orchestrator.process_turn(
                    self.arjun,
                    _request("speech", "text", audio_id=upload["audio_upload_id"]),
                )
        self.assertEqual(response.transcript, "ನನ್ನ balance")
        self.assertEqual([item.language for item in response.detected_languages], ["kn-IN", "en-IN"])
        self.assertTrue(response.is_code_mixed)
        self.assertEqual(response.response_language, "en-IN")
        self.assertFalse(observed_paths[0].exists())
        history = get_conversation_history(self.arjun, response.conversation_id)
        self.assertEqual(history["items"][0]["user_text_or_transcript"], "ನನ್ನ balance")
        self.assertEqual(history["items"][0]["input_type"], "audio")
        self.assertNotIn("audio_path", history["items"][0])

    def test_groq_transcript_uses_existing_assistant_turn_response_contract(self):
        upload = self._store_upload()
        create = Mock(return_value=SimpleNamespace(text="What is my balance?"))
        client = SimpleNamespace(
            audio=SimpleNamespace(
                transcriptions=SimpleNamespace(create=create)
            )
        )
        orchestrator = AssistantOrchestrator(
            transcriber=GroqWhisperTranscriber(client=client),
            synthesizer=GnaniTimbreSynthesizer(),
        )

        with patch("services.assistant_service.execute_tool", return_value=[]):
            response = orchestrator.process_turn(
                self.arjun,
                _request("speech", "text", audio_id=upload["audio_upload_id"]),
            )

        self.assertEqual(response.transcript, "What is my balance?")
        self.assertEqual(
            [item.language for item in response.detected_languages], ["en-IN"]
        )
        self.assertEqual(create.call_args.kwargs["language"], "en")

    def test_text_input_to_speech_and_both_use_the_same_response_text(self):
        text_values, languages, voices = [], [], []
        timbre = self._mock_timbre(text_values, languages, voices)
        with patch.dict("os.environ", {"VAANISHIELD_TIMBRE_VOICE_EN_IN": "TestEnglishVoice"}):
            with patch("services.speech_adapters.gnani_speech.synthesize_speech", side_effect=timbre):
                with patch("services.assistant_service.execute_tool", return_value=[]):
                    speech_response = self.orchestrator.process_turn(
                        self.arjun, _request("text", "speech")
                    )
                    both_response = self.orchestrator.process_turn(
                        self.arjun, _request("text", "speech_and_text")
                    )
        self.assertEqual(speech_response.assistant_text, text_values[0])
        self.assertIsNotNone(speech_response.speech_output)
        self.assertEqual(both_response.assistant_text, text_values[1])
        self.assertEqual(speech_response.speech_output.language, "en-IN")
        self.assertEqual(languages, ["en-IN", "en-IN"])
        self.assertEqual(voices, ["TestEnglishVoice", "TestEnglishVoice"])
        for audio in (speech_response.speech_output, both_response.speech_output):
            self.artifact_ids.append(UUID(audio.audio_url.rsplit("/", 1)[-1]))
        self.assertIsNone(speech_storage.get_speech_artifact(self.priya, self.artifact_ids[0]))
        response = retrieve_assistant_audio(self.artifact_ids[0], self.arjun)
        self.assertIsInstance(response, FileResponse)
        self.assertTrue(Path(response.path).exists())

        hindi_text, hindi_languages, hindi_voices = [], [], []
        with patch.dict("os.environ", {"VAANISHIELD_TIMBRE_VOICE_HI_IN": "TestHindiVoice"}):
            with patch(
                "services.speech_adapters.gnani_speech.synthesize_speech",
                side_effect=self._mock_timbre(hindi_text, hindi_languages, hindi_voices),
            ):
                with patch("services.assistant_service.execute_tool", return_value=[]):
                    hindi = self.orchestrator.process_turn(
                        self.arjun,
                        AssistantTurnRequest(
                            client_turn_id=uuid4(), input_mode="text",
                            output_mode="speech_and_text", text="What is my balance?",
                            response_language="hi-IN",
                        ),
                    )
        self.assertEqual(hindi.response_language, "hi-IN")
        self.assertEqual(hindi.assistant_text, hindi_text[0])
        self.assertEqual(hindi_languages, ["hi-IN"])
        self.assertEqual(hindi_voices, ["TestHindiVoice"])
        self.artifact_ids.append(UUID(hindi.speech_output.audio_url.rsplit("/", 1)[-1]))

    def test_speech_input_with_both_outputs(self):
        upload = self._store_upload()
        text_values, languages, voices = [], [], []
        with patch.dict("os.environ", {"VAANISHIELD_TIMBRE_VOICE_KN_IN": "KannadaVoice"}):
            with patch(
                "services.speech_adapters.gnani_speech.transcribe_audio",
                return_value={"transcript": "ನನ್ನ ಬ್ಯಾಲೆನ್ಸ್", "raw_response": {}},
            ):
                with patch(
                    "services.speech_adapters.gnani_speech.synthesize_speech",
                    side_effect=self._mock_timbre(text_values, languages, voices),
                ):
                    with patch("services.assistant_service.execute_tool", return_value=[]):
                        response = self.orchestrator.process_turn(
                            self.arjun,
                            _request("speech", "speech_and_text", audio_id=upload["audio_upload_id"]),
                        )
        self.assertEqual(response.response_language, "kn-IN")
        self.assertEqual(response.assistant_text, text_values[0])
        self.assertEqual(response.speech_output.language, "kn-IN")
        self.assertEqual(voices, ["KannadaVoice"])
        self.artifact_ids.append(UUID(response.speech_output.audio_url.rsplit("/", 1)[-1]))

    def test_upload_route_rejects_unsupported_and_oversized_audio(self):
        bad = UploadFile(
            filename="payload.txt", file=BytesIO(b"not audio"),
            headers=Headers({"content-type": "text/plain"}),
        )
        with self.assertRaises(HTTPException) as unsupported:
            import asyncio
            asyncio.run(upload_assistant_audio(bad, self.arjun))
        self.assertEqual(unsupported.exception.status_code, 415)

        too_big = UploadFile(
            filename="large.wav", file=BytesIO(_wav_bytes() + b"0" * speech_storage.MAX_AUDIO_SIZE),
            headers=Headers({"content-type": "audio/wav"}),
        )
        with self.assertRaises(HTTPException) as oversized:
            asyncio.run(upload_assistant_audio(too_big, self.arjun))
        self.assertEqual(oversized.exception.status_code, 413)

    def test_provider_failures_are_sanitized_and_input_audio_is_removed(self):
        upload = self._store_upload()
        with patch(
            "services.speech_adapters.gnani_speech.transcribe_audio",
            side_effect=RuntimeError("secret provider diagnostics"),
        ):
            with self.assertRaises(InputProcessingUnavailable) as failure:
                self.orchestrator.process_turn(
                    self.arjun,
                    _request("speech", "text", audio_id=upload["audio_upload_id"]),
                )
        self.assertNotIn("secret provider", str(failure.exception))
        with self.assertRaises(speech_storage.AudioUploadError):
            with speech_storage.consume_audio_upload(self.arjun, upload["audio_upload_id"]):
                pass

        with patch.dict("os.environ", {"VAANISHIELD_TIMBRE_VOICE_EN_IN": "TestVoice"}):
            with patch(
                "services.speech_adapters.gnani_speech.synthesize_speech",
                side_effect=RuntimeError("secret provider diagnostics"),
            ):
                with patch("services.assistant_service.execute_tool", return_value=[]):
                    response = self.orchestrator.process_turn(
                        self.arjun, _request("text", "speech")
                    )
        self.assertTrue(response.assistant_text)
        self.assertIsNone(response.speech_output)

    def test_uploads_are_customer_scoped(self):
        upload = self._store_upload()
        with self.assertRaises(speech_storage.AudioUploadError):
            with speech_storage.consume_audio_upload(self.priya, upload["audio_upload_id"]):
                pass
        with speech_storage.consume_audio_upload(self.arjun, upload["audio_upload_id"]) as path:
            self.assertTrue(path.exists())
        self.assertFalse(path.exists())


if __name__ == "__main__":
    unittest.main()
