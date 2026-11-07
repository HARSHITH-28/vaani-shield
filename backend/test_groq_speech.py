"""Offline tests for the Groq Whisper speech adapter."""

from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

from services.speech_adapters import (
    GroqWhisperTranscriber,
    create_speech_transcriber,
)
from services.speech_interfaces import AudioUploadUnavailable, InputProcessingUnavailable


class GroqWhisperTranscriberTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.upload_id = uuid4()
        self.create = Mock(return_value=SimpleNamespace(text="  English transcript  "))
        self.client = SimpleNamespace(
            audio=SimpleNamespace(
                transcriptions=SimpleNamespace(create=self.create)
            )
        )
        self.transcriber = GroqWhisperTranscriber(client=self.client)

    def tearDown(self):
        self.temp_dir.cleanup()

    @contextmanager
    def _uploaded_file(self, suffix):
        path = self.root / f"recording{suffix}"
        path.write_bytes(b"audio bytes")
        yield path

    def _transcribe(self, suffix=".wav", language_hint="auto"):
        with patch(
            "services.speech_adapters.consume_audio_upload",
            return_value=self._uploaded_file(suffix),
        ):
            return self.transcriber.transcribe(object(), self.upload_id, language_hint)

    def test_success_uses_whisper_large_v3_and_forces_english(self):
        result = self._transcribe(language_hint="hi-IN")

        self.assertEqual(result.text, "English transcript")
        self.assertEqual(result.languages, ("en-IN",))
        self.create.assert_called_once()
        kwargs = self.create.call_args.kwargs
        self.assertEqual(kwargs["model"], "whisper-large-v3")
        self.assertEqual(kwargs["language"], "en")
        self.assertEqual(kwargs["response_format"], "json")
        self.assertEqual(kwargs["file"], ("recording.wav", b"audio bytes"))

    def test_all_existing_upload_formats_are_passed_through(self):
        for suffix in (".wav", ".mp3", ".ogg", ".webm"):
            with self.subTest(suffix=suffix):
                self.create.reset_mock()
                result = self._transcribe(suffix)
                self.assertEqual(result.text, "English transcript")
                self.assertEqual(self.create.call_args.kwargs["file"][0], f"recording{suffix}")

    def test_empty_transcription_is_rejected(self):
        self.create.return_value = SimpleNamespace(text="  ")
        with self.assertRaises(InputProcessingUnavailable) as error:
            self._transcribe()
        self.assertEqual(error.exception.code, "EMPTY_TRANSCRIPT")

    def test_unsupported_audio_is_rejected_without_provider_call(self):
        with self.assertRaises(AudioUploadUnavailable):
            self._transcribe(".flac")
        self.create.assert_not_called()

    def test_provider_error_is_sanitized(self):
        self.create.side_effect = RuntimeError("provider detail with private content")
        with self.assertRaises(InputProcessingUnavailable) as error:
            self._transcribe()
        self.assertEqual(error.exception.code, "SPEECH_PROVIDER_ERROR")
        self.assertNotIn("private content", str(error.exception))

    def test_missing_key_is_reported_without_provider_call(self):
        transcriber = GroqWhisperTranscriber(api_key="")
        with patch.dict("os.environ", {"GROQ_API_KEY": ""}):
            with patch(
                "services.speech_adapters.consume_audio_upload",
                return_value=self._uploaded_file(".wav"),
            ):
                with self.assertRaises(InputProcessingUnavailable) as error:
                    transcriber.transcribe(object(), self.upload_id, "auto")
        self.assertEqual(error.exception.code, "SPEECH_NOT_CONFIGURED")

    def test_speech_provider_selection_is_independent(self):
        with patch.dict("os.environ", {"VAANISHIELD_STT_PROVIDER": "groq"}):
            self.assertIsInstance(create_speech_transcriber(), GroqWhisperTranscriber)
        with patch.dict("os.environ", {"VAANISHIELD_STT_PROVIDER": "prisma"}):
            from services.speech_adapters import GnaniPrismaTranscriber

            self.assertIsInstance(create_speech_transcriber(), GnaniPrismaTranscriber)


if __name__ == "__main__":
    unittest.main()
