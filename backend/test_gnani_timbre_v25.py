"""Offline tests for Gnani Timbre v2.5 REST synthesis."""

from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch
from uuid import UUID, uuid4

import requests

from backend.assistant_voice_routes import retrieve_assistant_audio
from services import gnani_speech, speech_storage
from services.principal import CustomerPrincipal
from services.speech_adapters import (
    GnaniTimbreSynthesizer,
    GnaniTimbreV25Synthesizer,
    create_speech_synthesizer,
)
from services.speech_interfaces import OutputProcessingUnavailable


def _wav_bytes():
    return b"RIFF" + b"\x00\x00\x00\x00" + b"WAVE" + b"fmt " + b"\x00\x00\x00\x00"


def _principal():
    now = datetime.now(timezone.utc)
    return CustomerPrincipal(
        "demo-arjun", uuid4(), now - timedelta(minutes=1), now + timedelta(minutes=20)
    )


class GnaniTimbreV25Tests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = TemporaryDirectory()
        self.audio_dir_patch = patch.object(
            gnani_speech, "AUDIO_DIR", Path(self.temp_dir.name) / "audio"
        )
        self.audio_dir_patch.start()
        Path(gnani_speech.AUDIO_DIR).mkdir(parents=True, exist_ok=True)
        self.principal = _principal()
        self.artifact_ids = []
        self.synthesizer = GnaniTimbreV25Synthesizer()

    def tearDown(self):
        for artifact_id in self.artifact_ids:
            speech_storage.delete_speech_artifact(artifact_id)
        self.audio_dir_patch.stop()
        self.temp_dir.cleanup()

    def _mock_provider(self, *, content=None, side_effect=None):
        return patch(
            "services.speech_adapters.gnani_speech.synthesize_speech_v25",
            Mock(return_value=content if content is not None else _wav_bytes(), side_effect=side_effect),
        )

    def test_success_returns_owned_stored_wav_audio(self):
        with patch.object(gnani_speech, "API_KEY", "mock-key"):
            with self._mock_provider() as synthesize:
                output = self.synthesizer.synthesize(
                    self.principal, "Your demo balance is available.", "en-IN"
                )

        synthesize.assert_called_once_with(
            "Your demo balance is available.", language="en-IN", voice="Kaveri"
        )
        self.assertEqual(output.media_type, "audio/wav")
        artifact_id = UUID(output.audio_url.rsplit("/", 1)[-1])
        self.artifact_ids.append(artifact_id)
        response = retrieve_assistant_audio(artifact_id, self.principal)
        self.assertEqual(response.media_type, "audio/wav")
        self.assertEqual(Path(response.path).read_bytes(), _wav_bytes())

    def test_missing_key_does_not_call_provider(self):
        with patch.object(gnani_speech, "API_KEY", ""):
            with self._mock_provider() as synthesize:
                with self.assertRaises(OutputProcessingUnavailable) as failure:
                    self.synthesizer.synthesize(self.principal, "Hello", "en-IN")
        self.assertEqual(failure.exception.code, "TTS_NOT_CONFIGURED")
        synthesize.assert_not_called()

    def test_empty_or_malformed_audio_is_rejected(self):
        for audio in (b"", b"not a wav file", b"RIFF" + b"\x00" * 8):
            with self.subTest(audio=audio[:8]):
                with patch.object(gnani_speech, "API_KEY", "mock-key"):
                    with self._mock_provider(content=audio):
                        with self.assertRaises(OutputProcessingUnavailable) as failure:
                            self.synthesizer.synthesize(self.principal, "Hello", "en-IN")
                self.assertEqual(failure.exception.code, "TTS_INVALID_AUDIO")
        self.assertEqual(list(Path(gnani_speech.AUDIO_DIR).glob("assistant_*.wav")), [])

    def test_authentication_and_rate_limit_errors_are_sanitized(self):
        for status, expected_code in ((401, "TTS_CREDENTIALS_REJECTED"), (403, "TTS_CREDENTIALS_REJECTED"), (429, "TTS_RATE_LIMITED")):
            with self.subTest(status=status):
                response = requests.Response()
                response.status_code = status
                error = requests.HTTPError("sensitive provider response", response=response)
                with patch.object(gnani_speech, "API_KEY", "mock-key"):
                    with self._mock_provider(side_effect=error):
                        with self.assertRaises(OutputProcessingUnavailable) as failure:
                            self.synthesizer.synthesize(self.principal, "Hello", "en-IN")
                self.assertEqual(failure.exception.code, expected_code)
                self.assertNotIn("sensitive provider", str(failure.exception))

    def test_timeout_and_network_failures_are_sanitized(self):
        for error, expected_code in (
            (requests.Timeout("private timeout data"), "TTS_TIMEOUT"),
            (requests.ConnectionError("private connection data"), "TTS_CONNECTION_ERROR"),
        ):
            with self.subTest(error=type(error).__name__):
                with patch.object(gnani_speech, "API_KEY", "mock-key"):
                    with self._mock_provider(side_effect=error):
                        with self.assertRaises(OutputProcessingUnavailable) as failure:
                            self.synthesizer.synthesize(self.principal, "Hello", "en-IN")
                self.assertEqual(failure.exception.code, expected_code)
                self.assertNotIn("private", str(failure.exception))

    def test_unsupported_language_and_voice_are_rejected(self):
        with self.assertRaises(OutputProcessingUnavailable) as language_failure:
            self.synthesizer.synthesize(self.principal, "Hello", "fr-FR")
        self.assertEqual(language_failure.exception.code, "UNSUPPORTED_TTS_LANGUAGE")

        with patch.dict("os.environ", {"VAANISHIELD_TIMBRE_VOICE_EN_IN": "Shivani"}):
            with self.assertRaises(OutputProcessingUnavailable) as voice_failure:
                self.synthesizer.synthesize(self.principal, "Hello", "en-IN")
        self.assertEqual(voice_failure.exception.code, "UNSUPPORTED_TTS_VOICE")

    def test_provider_selection_is_independent_of_stt_and_llm(self):
        with patch.dict(
            "os.environ",
            {
                "VAANISHIELD_TTS_PROVIDER": "gnani",
                "VAANISHIELD_STT_PROVIDER": "groq",
                "VAANISHIELD_LLM_PROVIDER": "groq",
            },
        ):
            self.assertIsInstance(create_speech_synthesizer(), GnaniTimbreV25Synthesizer)
        with patch.dict("os.environ", {"VAANISHIELD_TTS_PROVIDER": "gnani_v3_legacy"}):
            self.assertIsInstance(create_speech_synthesizer(), GnaniTimbreSynthesizer)


class GnaniTimbreV25RequestTests(unittest.TestCase):
    def test_wrapper_uses_current_official_json_request_and_binary_response(self):
        response = Mock()
        response.content = _wav_bytes()
        with patch.object(gnani_speech, "API_KEY", "mock-key"):
            with patch.object(gnani_speech.requests, "post", return_value=response) as post:
                audio = gnani_speech.synthesize_speech_v25(
                    "Hello from VaaniShield", language="en-IN", voice="Kaveri"
                )

        self.assertEqual(audio, _wav_bytes())
        args, kwargs = post.call_args
        self.assertEqual(args[0], "https://api.vachana.ai/api/v1/tts/inference")
        self.assertEqual(
            kwargs["headers"],
            {"X-API-Key-ID": "mock-key", "Content-Type": "application/json"},
        )
        self.assertEqual(
            kwargs["json"],
            {
                "text": "Hello from VaaniShield",
                "voice": "Kaveri",
                "model": "timbre-v2.5",
                "language": "en-IN",
                "language_code": "en-IN",
                "lang": "en-IN",
                "audio_config": {
                    "sample_rate": 48000,
                    "num_channels": 1,
                    "sample_width": 2,
                    "encoding": "linear_pcm",
                    "container": "wav",
                },
            },
        )
        self.assertEqual(kwargs["timeout"], 60)


if __name__ == "__main__":
    unittest.main()
