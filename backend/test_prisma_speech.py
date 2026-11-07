"""Mocked tests for Gnani Prisma REST transcription; no network calls are made."""

from contextlib import contextmanager
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

import requests

from services import gnani_speech
from services.speech_adapters import GnaniPrismaTranscriber
from services.speech_interfaces import InputProcessingUnavailable


class GnaniPrismaTranscriberTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.upload_id = uuid4()
        self.transcriber = GnaniPrismaTranscriber()

    def tearDown(self):
        self.temp_dir.cleanup()

    @contextmanager
    def _uploaded_file(self, suffix=".wav"):
        path = self.root / f"recording{suffix}"
        path.write_bytes(b"mock audio data")
        yield path

    def _transcribe_with_result(self, result, *, suffix=".wav", language_hint="auto"):
        with patch.dict(os.environ, {}, clear=True):
            with patch.object(gnani_speech, "API_KEY", "mock-key"):
                with patch(
                    "services.speech_adapters.consume_audio_upload",
                    return_value=self._uploaded_file(suffix),
                ):
                    with patch(
                        "services.speech_adapters.gnani_speech.transcribe_audio",
                        return_value=result,
                    ) as call:
                        transcription = self.transcriber.transcribe(
                            object(), self.upload_id, language_hint
                        )
        return transcription, call

    def test_success_uses_auto_language_and_preserves_detected_language(self):
        result, call = self._transcribe_with_result(
            {
                "transcript": "My account balance",
                "raw_response": {
                    "success": True,
                    "detected_language_code": "en-IN",
                },
            }
        )

        self.assertEqual(result.text, "My account balance")
        self.assertEqual(result.languages, ("en-IN",))
        call.assert_called_once()
        self.assertEqual(call.call_args.kwargs["language_code"], "en-IN,kn-IN,hi-IN")
        self.assertEqual(call.call_args.kwargs["output_mode"], "transcribe")
        self.assertIn("UPI", call.call_args.kwargs["keyterms"])
        self.assertIn("financial security", call.call_args.kwargs["topic"])
        self.assertEqual(
            call.call_args.kwargs["speaker_names"],
            ("Arjun Sharma", "Priya Nair", "Rahul Verma"),
        )

    def test_explicit_language_hint_is_forwarded(self):
        _, call = self._transcribe_with_result(
            {"transcript": "ನಮಸ್ಕಾರ", "raw_response": {"detected_language_code": "kn-IN"}},
            language_hint="kn-IN",
        )
        self.assertEqual(call.call_args.kwargs["language_code"], "kn-IN")

    def test_empty_transcript_is_rejected(self):
        with self.assertRaises(InputProcessingUnavailable):
            self._transcribe_with_result({"transcript": "  ", "raw_response": {}})

    def test_provider_failure_is_sanitized(self):
        with patch.object(gnani_speech, "API_KEY", "mock-key"):
            with patch(
                "services.speech_adapters.consume_audio_upload",
                return_value=self._uploaded_file(),
            ):
                with patch(
                    "services.speech_adapters.gnani_speech.transcribe_audio",
                    side_effect=RuntimeError("private provider response"),
                ):
                    with self.assertRaises(InputProcessingUnavailable) as failure:
                        self.transcriber.transcribe(object(), self.upload_id, "auto")
        self.assertNotIn("private provider response", str(failure.exception))

    def test_http_rate_limit_is_mapped_without_provider_body(self):
        response = Mock(status_code=429)
        error = requests.HTTPError("private provider details")
        error.response = response
        with patch.object(gnani_speech, "API_KEY", "mock-key"):
            with patch(
                "services.speech_adapters.consume_audio_upload",
                return_value=self._uploaded_file(),
            ):
                with patch(
                    "services.speech_adapters.gnani_speech.transcribe_audio",
                    side_effect=error,
                ):
                    with self.assertRaises(InputProcessingUnavailable) as failure:
                        self.transcriber.transcribe(object(), self.upload_id, "auto")
        self.assertEqual(failure.exception.code, "SPEECH_RATE_LIMITED")
        self.assertNotIn("private provider", str(failure.exception))

    def test_unsupported_webm_is_rejected_before_provider_request(self):
        with patch.object(gnani_speech, "API_KEY", "mock-key"):
            with patch(
                "services.speech_adapters.consume_audio_upload",
                return_value=self._uploaded_file(".webm"),
            ):
                with patch(
                    "services.speech_adapters.gnani_speech.transcribe_audio"
                ) as call:
                    with self.assertRaises(InputProcessingUnavailable) as failure:
                        self.transcriber.transcribe(object(), self.upload_id, "auto")
        self.assertEqual(failure.exception.code, "UNSUPPORTED_AUDIO_FORMAT")
        call.assert_not_called()

    def test_missing_gnani_key_is_reported_without_provider_request(self):
        with patch.object(gnani_speech, "API_KEY", None):
            with patch("services.speech_adapters.gnani_speech.transcribe_audio") as call:
                with self.assertRaises(InputProcessingUnavailable) as failure:
                    self.transcriber.transcribe(object(), self.upload_id, "auto")
        self.assertEqual(failure.exception.code, "SPEECH_NOT_CONFIGURED")
        call.assert_not_called()


class GnaniPrismaRestRequestTests(unittest.TestCase):
    def test_wrapper_uses_documented_prisma_v3_multipart_contract(self):
        with TemporaryDirectory() as directory:
            audio_path = Path(directory) / "sample.wav"
            audio_path.write_bytes(b"mock audio")
            response = Mock()
            response.json.return_value = {
                "success": True,
                "transcript": "Hello",
                "detected_language_code": "en-IN",
            }
            with patch.object(gnani_speech, "API_KEY", "mock-key"):
                with patch.object(gnani_speech.requests, "post", return_value=response) as post:
                    result = gnani_speech.transcribe_audio(audio_path, "indic-auto")

        self.assertEqual(result["transcript"], "Hello")
        args, kwargs = post.call_args
        self.assertEqual(args[0], "https://api.vachana.ai/stt/v3")
        self.assertEqual(kwargs["headers"], {"X-API-Key-ID": "mock-key"})
        self.assertEqual(
            kwargs["data"],
            {"model": "prisma-v3.0", "language_code": "indic-auto", "format": "transcribe"},
        )
        self.assertEqual(set(kwargs["files"]), {"audio_file"})
        self.assertEqual(kwargs["timeout"], 60)

    def test_wrapper_sends_documented_json_context_fields(self):
        response = Mock()
        response.json.return_value = {"success": True, "transcript": "Hello"}
        with TemporaryDirectory() as directory:
            audio_path = Path(directory) / "sample.wav"
            audio_path.write_bytes(b"mock audio")
            with patch.object(gnani_speech, "API_KEY", "mock-key"):
                with patch.object(gnani_speech.requests, "post", return_value=response) as post:
                    gnani_speech.transcribe_audio(
                        audio_path,
                        "en-IN,kn-IN,hi-IN",
                        output_mode="codemix",
                        keyterms=("VaaniShield", "UPI", "Google Pay"),
                        topic="Banking and financial security.",
                        speaker_names=("Arjun Sharma", "Priya Nair"),
                    )

        data = post.call_args.kwargs["data"]
        self.assertEqual(data["language_code"], "en-IN,kn-IN,hi-IN")
        self.assertEqual(data["format"], "codemix")
        self.assertEqual(json.loads(data["keyterms"]), ["VaaniShield", "UPI", "Google Pay"])
        self.assertEqual(data["topic"], "Banking and financial security.")
        self.assertEqual(json.loads(data["speaker_names"]), ["Arjun Sharma", "Priya Nair"])

    def test_unsuccessful_response_does_not_embed_provider_body_in_error(self):
        response = Mock()
        response.json.return_value = {
            "success": False,
            "error": {"message": "private data from provider"},
        }
        with TemporaryDirectory() as directory:
            audio_path = Path(directory) / "sample.wav"
            audio_path.write_bytes(b"mock audio")
            with patch.object(gnani_speech, "API_KEY", "mock-key"):
                with patch.object(gnani_speech.requests, "post", return_value=response):
                    with self.assertRaises(RuntimeError) as failure:
                        gnani_speech.transcribe_audio(audio_path)
        self.assertNotIn("private data", str(failure.exception))


if __name__ == "__main__":
    unittest.main()
