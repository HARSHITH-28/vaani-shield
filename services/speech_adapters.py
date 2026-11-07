"""Speech provider adapters implementing provider-neutral interfaces."""

import logging
import os
from pathlib import Path
import re
from uuid import UUID, uuid4

import requests

from backend.api_models import SpeechOutput
from services import gnani_speech
from services.speech_interfaces import (
    AudioUploadUnavailable,
    InputProcessingUnavailable,
    OutputProcessingUnavailable,
    Transcription,
)
from services.speech_storage import (
    AudioUploadError,
    UnsupportedAudioFormatError,
    consume_audio_upload,
    register_speech_artifact,
)
from services.prisma_config import PrismaConfigurationError, get_prisma_request_settings

logger = logging.getLogger(__name__)


class GroqWhisperTranscriber:
    """English transcription through Groq's official Whisper API."""

    MODEL = "whisper-large-v3"
    LANGUAGE = "en"
    SUPPORTED_SUFFIXES = frozenset({".wav", ".mp3", ".ogg", ".webm"})

    def __init__(self, *, api_key: str | None = None, client=None):
        # Defer SDK import/client construction until a speech request is made.
        self._api_key = api_key if api_key is not None else os.getenv("GROQ_API_KEY", "")
        self._client = client

    def _get_client(self):
        if self._client is not None:
            return self._client
        if not self._api_key.strip():
            logger.warning("Groq Whisper transcription unavailable (credentials_not_configured)")
            raise InputProcessingUnavailable(
                "Speech recognition is not configured. Ask the administrator to check Groq settings.",
                code="SPEECH_NOT_CONFIGURED",
            )
        try:
            from groq import Groq

            self._client = Groq(api_key=self._api_key)
            return self._client
        except InputProcessingUnavailable:
            raise
        except Exception as exc:
            # Never include SDK exception text, which can carry request details.
            logger.warning("Groq Whisper client initialization failed (%s)", type(exc).__name__)
            raise InputProcessingUnavailable(
                "Speech recognition is unavailable right now.", code="SPEECH_PROVIDER_ERROR"
            ) from None

    def transcribe(self, principal, audio_upload_id: UUID, language_hint: str):
        try:
            with consume_audio_upload(principal, audio_upload_id) as path:
                if Path(path).suffix.lower() not in self.SUPPORTED_SUFFIXES:
                    raise UnsupportedAudioFormatError("Unsupported or invalid audio format.")
                client = self._get_client()
                with Path(path).open("rb") as audio_file:
                    result = client.audio.transcriptions.create(
                        file=(Path(path).name, audio_file.read()),
                        model=self.MODEL,
                        language=self.LANGUAGE,
                        response_format="json",
                    )
            transcript = result.get("text") if isinstance(result, dict) else getattr(result, "text", None)
            if not isinstance(transcript, str) or not transcript.strip():
                raise InputProcessingUnavailable(
                    "Speech transcription returned no transcript.", code="EMPTY_TRANSCRIPT"
                )
            return Transcription(text=transcript.strip(), languages=("en-IN",))
        except AudioUploadError as exc:
            logger.warning("Groq Whisper audio unavailable (%s)", type(exc).__name__)
            raise AudioUploadUnavailable(
                "Audio upload is unavailable, expired, or unsupported.",
                code="AUDIO_UPLOAD_UNAVAILABLE",
            ) from None
        except InputProcessingUnavailable:
            raise
        except Exception as exc:
            # Do not log exception text, audio metadata, or provider response data.
            logger.warning("Groq Whisper transcription failed (%s)", type(exc).__name__)
            raise InputProcessingUnavailable(
                "Speech recognition could not process the recording. Please try again later.",
                code="SPEECH_PROVIDER_ERROR",
            ) from None


def create_speech_transcriber():
    """Select speech recognition independently from the LLM provider."""
    provider = os.getenv("VAANISHIELD_STT_PROVIDER", "prisma").strip().casefold()
    if provider == "groq":
        return GroqWhisperTranscriber()
    if provider == "prisma":
        return GnaniPrismaTranscriber()
    raise ValueError("VAANISHIELD_STT_PROVIDER must be 'prisma' or 'groq'.")


class GnaniPrismaTranscriber:
    """Call the existing Prisma wrapper and retain only safe transcript data."""

    SUPPORTED_SUFFIXES = frozenset({".wav", ".mp3", ".ogg", ".flac", ".aac", ".m4a"})

    def transcribe(self, principal, audio_upload_id: UUID, language_hint: str):
        if not gnani_speech.API_KEY or gnani_speech.API_KEY.casefold().startswith("replace_with"):
            logger.warning("Prisma transcription unavailable (credentials_not_configured)")
            raise InputProcessingUnavailable(
                "Speech recognition is not configured. Ask the administrator to check Gnani settings.",
                code="SPEECH_NOT_CONFIGURED",
            )
        try:
            with consume_audio_upload(principal, audio_upload_id) as path:
                if Path(path).suffix.lower() not in self.SUPPORTED_SUFFIXES:
                    raise UnsupportedAudioFormatError("Unsupported or invalid audio format.")
                settings = get_prisma_request_settings()
                result = gnani_speech.transcribe_audio(
                    path,
                    language_code=settings.language_code_for(language_hint),
                    output_mode=settings.output_mode,
                    keyterms=settings.keyterms,
                    topic=settings.topic,
                    speaker_names=settings.speaker_names,
                )
            transcript = result.get("transcript")
            if not isinstance(transcript, str) or not transcript.strip():
                raise InputProcessingUnavailable(
                    "Speech transcription returned no transcript.",
                    code="EMPTY_TRANSCRIPT",
                )
            raw = result.get("raw_response")
            raw = raw if isinstance(raw, dict) else {}
            reported = raw.get(
                "detected_language_code",
                raw.get("detected_languages", raw.get("languages", [])),
            )
            if isinstance(reported, str):
                reported = [reported]
            supported = {"kn-IN", "en-IN", "hi-IN"}
            languages = []
            if isinstance(reported, (list, tuple)):
                for item in reported:
                    code = item.get("language", item.get("code")) if isinstance(item, dict) else item
                    if code in supported and code not in languages:
                        languages.append(code)
            clean_text = transcript.strip()
            kn_chars = sum(1 for c in clean_text if 0x0C80 <= ord(c) <= 0x0CFF)
            hi_chars = sum(1 for c in clean_text if 0x0900 <= ord(c) <= 0x097F)
            en_chars = sum(1 for c in clean_text if c.isascii() and c.isalpha())
            if en_chars >= 4 and hi_chars == 0 and kn_chars == 0:
                words = set(re.findall(r"[a-z]+", clean_text.casefold()))
                hi_words = len(words & {"mera", "meri", "mere", "mein", "hai", "hain", "kitna", "kya", "dikhao", "batao", "kripya", "mujhe", "ka", "ki", "ke"})
                kn_words = len(words & {"nanna", "nimma", "alli", "ide", "beku", "eshtu", "yenu", "helri", "torisi", "maadi", "ge", "inda"})
                if hi_words < 2 and kn_words < 2:
                    if "en-IN" not in languages:
                        languages.insert(0, "en-IN")
                    if "hi-IN" in languages and len(languages) > 1:
                        languages.remove("hi-IN")
            elif hi_chars >= 4 and "hi-IN" not in languages:
                languages.insert(0, "hi-IN")
            elif kn_chars >= 4 and "kn-IN" not in languages:
                languages.insert(0, "kn-IN")
            if not languages:
                languages = ["en-IN"]
            # Raw provider payloads and diagnostics are intentionally discarded.
            return Transcription(
                text=transcript.strip(),
                languages=tuple(languages),
                is_code_mixed=bool(raw.get("is_code_mixed", False)),
            )
        except PrismaConfigurationError:
            logger.warning("Prisma transcription unavailable (configuration_invalid)")
            raise InputProcessingUnavailable(
                "Prisma speech recognition configuration is invalid.",
                code="PRISMA_CONFIGURATION_INVALID",
            ) from None
        except UnsupportedAudioFormatError:
            logger.warning("Prisma transcription unavailable (unsupported_audio_format)")
            raise InputProcessingUnavailable(
                "Prisma does not support this audio format. Use WAV, MP3, OGG, FLAC, AAC, or M4A.",
                code="UNSUPPORTED_AUDIO_FORMAT",
            ) from None
        except AudioUploadError:
            raise AudioUploadUnavailable(
                "Audio upload is unavailable or expired.", code="AUDIO_UPLOAD_UNAVAILABLE"
            ) from None
        except InputProcessingUnavailable:
            raise
        except requests.Timeout:
            logger.warning("Prisma transcription failed (timeout)")
            raise InputProcessingUnavailable(
                "Speech recognition timed out. Please try again.", code="SPEECH_TIMEOUT"
            ) from None
        except requests.HTTPError as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            logger.warning("Prisma transcription failed (http_status=%s)", status or "unknown")
            if status in {401, 403}:
                message, code = "Speech recognition credentials were rejected. Ask the administrator to check Gnani settings.", "SPEECH_CREDENTIALS_REJECTED"
            elif status == 429:
                message, code = "Speech recognition is temporarily rate limited. Please try again shortly.", "SPEECH_RATE_LIMITED"
            else:
                message, code = "Speech recognition provider returned an error. Please try again later.", "SPEECH_PROVIDER_ERROR"
            raise InputProcessingUnavailable(message, code=code) from None
        except requests.ConnectionError:
            logger.warning("Prisma transcription failed (connection_error)")
            raise InputProcessingUnavailable(
                "Speech recognition could not connect to its provider. Please try again later.",
                code="SPEECH_CONNECTION_ERROR",
            ) from None
        except Exception as exc:
            logger.warning("Prisma transcription failed (%s)", type(exc).__name__)
            raise InputProcessingUnavailable(
                "Speech recognition could not process the recording. Check Gnani configuration or try again later.",
                code="SPEECH_PROVIDER_ERROR",
            ) from None


class GnaniTimbreSynthesizer:
    """Use the existing Timbre wrapper and return an authenticated audio URL."""

    VOICE_ENV = {
        "en-IN": "VAANISHIELD_TIMBRE_VOICE_EN_IN",
        "hi-IN": "VAANISHIELD_TIMBRE_VOICE_HI_IN",
    }

    def _voice_for_language(self, language: str) -> str:
        if language == "kn-IN":
            # Shivani is the voice already used by the legacy speech route.
            voice = os.getenv("VAANISHIELD_TIMBRE_VOICE_KN_IN", "Shivani").strip()
            if not voice or voice.casefold().startswith("replace_with"):
                raise OutputProcessingUnavailable(
                    "Speech output is not configured for the selected language."
                )
            return voice
        env_name = self.VOICE_ENV.get(language)
        if env_name is None:
            raise OutputProcessingUnavailable(
                "Speech output is unavailable for the selected language."
            )
        voice = os.getenv(env_name, "").strip()
        if not voice or voice.casefold().startswith("replace_with"):
            raise OutputProcessingUnavailable(
                "Speech output is not configured for the selected language."
            )
        return voice

    def synthesize(self, principal, text: str, language: str) -> SpeechOutput:
        voice = self._voice_for_language(language)
        filename = f"assistant_{uuid4().hex}.wav"
        result = None
        output_path = None
        artifact_registered = False
        try:
            result = gnani_speech.synthesize_speech(
                text,
                language=language,
                voice=voice,
                filename=filename,
            )
            output_path = Path(result["audio_path"])
            allowed_root = Path(gnani_speech.AUDIO_DIR).resolve()
            if output_path.resolve().parent != allowed_root:
                raise OutputProcessingUnavailable(
                    "Generated speech output is unavailable."
                )
            artifact_id, expires_at = register_speech_artifact(
                principal,
                output_path,
                allowed_root,
                media_type="audio/wav",
                language=language,
            )
            artifact_registered = True
            return SpeechOutput(
                audio_url=f"/api/v1/voice/responses/{artifact_id}",
                media_type="audio/wav",
                language=language,
                expires_at=expires_at,
            )
        except OutputProcessingUnavailable:
            raise
        except Exception:
            raise OutputProcessingUnavailable(
                "Speech output is unavailable right now."
            ) from None
        finally:
            # If artifact registration did not succeed, remove the generated WAV.
            if output_path is not None and result is not None:
                try:
                    if output_path.exists() and output_path.name == filename:
                        if output_path.resolve().parent == Path(gnani_speech.AUDIO_DIR).resolve():
                            if not artifact_registered:
                                output_path.unlink(missing_ok=True)
                except OSError:
                    pass


class GnaniTimbreV25Synthesizer:
    """Synchronous Timbre v2.5 WAV synthesis through the official REST wrapper."""

    MODEL = "timbre-v2.5"
    AUDIO_FORMAT = "audio/wav"
    DEFAULT_VOICES = {
        "en-IN": "Kaveri",
        "hi-IN": "Nalini",
        "kn-IN": "Kavin",
    }
    SUPPORTED_VOICES = {
        "en-IN": frozenset({"Kaveri", "Trupti", "Devika", "Pranav", "Shlok", "Girish"}),
        "hi-IN": frozenset({
            "Nalini", "Bhavna", "Yashvi", "Urmila", "Jwala", "Chitra", "Ambuja",
            "Deepak", "Roopesh", "Vikrant", "Hemraj", "Jalaj", "Omkar",
        }),
        "kn-IN": frozenset({"Saanvi", "Kavin"}),
    }
    VOICE_ENV = {
        "en-IN": "VAANISHIELD_TIMBRE_VOICE_EN_IN",
        "hi-IN": "VAANISHIELD_TIMBRE_VOICE_HI_IN",
        "kn-IN": "VAANISHIELD_TIMBRE_VOICE_KN_IN",
    }

    def _voice_for_language(self, language: str) -> str:
        default = self.DEFAULT_VOICES.get(language)
        if default is None:
            raise OutputProcessingUnavailable(
                "Timbre speech output is unavailable for the selected language.",
                code="UNSUPPORTED_TTS_LANGUAGE",
            )
        configured = os.getenv(self.VOICE_ENV[language], "").strip()
        voice = default if not configured or configured.casefold().startswith("replace_with") else configured
        if voice not in self.SUPPORTED_VOICES[language]:
            raise OutputProcessingUnavailable(
                "Timbre speech output is not configured with a supported voice.",
                code="UNSUPPORTED_TTS_VOICE",
            )
        return voice

    def synthesize(self, principal, text: str, language: str) -> SpeechOutput:
        # Validate text language to ensure text script matches target TTS language
        kn_chars = sum(1 for c in text if 0x0C80 <= ord(c) <= 0x0CFF)
        hi_chars = sum(1 for c in text if 0x0900 <= ord(c) <= 0x097F)
        en_chars = sum(1 for c in text if c.isascii() and c.isalpha())
        
        target_lang = language
        if hi_chars >= 5 and hi_chars > en_chars:
            target_lang = "hi-IN"
        elif kn_chars >= 5 and kn_chars > en_chars:
            target_lang = "kn-IN"
        elif en_chars >= 5 and hi_chars == 0 and kn_chars == 0 and language in {"hi-IN", "kn-IN"}:
            words = set(re.findall(r"[a-z]+", text.casefold()))
            indic_words = len(words & {"hai", "hain", "aap", "mera", "meri", "kripya", "nanna", "nimma", "ide", "beku"})
            if indic_words < 2:
                # English text must not be spoken with a non-English voice
                target_lang = "en-IN"

        voice = self._voice_for_language(target_lang)
        if not gnani_speech.API_KEY or gnani_speech.API_KEY.casefold().startswith("replace_with"):
            logger.warning("Timbre v2.5 synthesis unavailable (credentials_not_configured)")
            raise OutputProcessingUnavailable(
                "Speech output is not configured. Ask the administrator to check Gnani settings.",
                code="TTS_NOT_CONFIGURED",
            )

        filename = f"assistant_{uuid4().hex}.wav"
        audio_path = Path(gnani_speech.AUDIO_DIR) / filename
        artifact_registered = False
        try:
            audio_bytes = gnani_speech.synthesize_speech_v25(
                text, language=target_lang, voice=voice
            )
            if (
                not isinstance(audio_bytes, bytes)
                or len(audio_bytes) < 12
                or audio_bytes[:4] != b"RIFF"
                or audio_bytes[8:12] != b"WAVE"
            ):
                raise OutputProcessingUnavailable(
                    "Timbre returned an invalid audio response.", code="TTS_INVALID_AUDIO"
                )
            audio_root = Path(gnani_speech.AUDIO_DIR).resolve()
            audio_root.mkdir(parents=True, exist_ok=True)
            if audio_path.resolve().parent != audio_root:
                raise OutputProcessingUnavailable("Generated speech output is unavailable.")
            audio_path.write_bytes(audio_bytes)
            artifact_id, expires_at = register_speech_artifact(
                principal,
                audio_path,
                audio_root,
                media_type=self.AUDIO_FORMAT,
                language=target_lang,
            )
            artifact_registered = True
            return SpeechOutput(
                audio_url=f"/api/v1/voice/responses/{artifact_id}",
                media_type=self.AUDIO_FORMAT,
                language=target_lang,
                expires_at=expires_at,
            )
        except OutputProcessingUnavailable:
            raise
        except requests.Timeout:
            logger.warning("Timbre v2.5 synthesis failed (timeout)")
            raise OutputProcessingUnavailable(
                "Speech generation timed out. Please try again.", code="TTS_TIMEOUT"
            ) from None
        except requests.HTTPError as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            logger.warning("Timbre v2.5 synthesis failed (http_status=%s)", status or "unknown")
            if status in {401, 403}:
                message, code = "Speech output credentials were rejected. Ask the administrator to check Gnani settings.", "TTS_CREDENTIALS_REJECTED"
            elif status == 429:
                message, code = "Speech generation is temporarily rate limited. Please try again shortly.", "TTS_RATE_LIMITED"
            else:
                message, code = "Speech output is unavailable right now.", "TTS_PROVIDER_ERROR"
            raise OutputProcessingUnavailable(message, code=code) from None
        except requests.ConnectionError:
            logger.warning("Timbre v2.5 synthesis failed (connection_error)")
            raise OutputProcessingUnavailable(
                "Speech generation could not connect to its provider. Please try again later.",
                code="TTS_CONNECTION_ERROR",
            ) from None
        except Exception as exc:
            logger.warning("Timbre v2.5 synthesis failed (%s)", type(exc).__name__)
            raise OutputProcessingUnavailable(
                "Speech output is unavailable right now.", code="TTS_PROVIDER_ERROR"
            ) from None
        finally:
            if not artifact_registered:
                try:
                    if audio_path.exists() and audio_path.resolve().parent == Path(gnani_speech.AUDIO_DIR).resolve():
                        audio_path.unlink(missing_ok=True)
                except OSError:
                    pass


def create_speech_synthesizer():
    """Select TTS separately from the STT and LLM providers."""
    provider = os.getenv("VAANISHIELD_TTS_PROVIDER", "gnani").strip().casefold()
    if provider == "gnani":
        return GnaniTimbreV25Synthesizer()
    if provider == "gnani_v3_legacy":
        return GnaniTimbreSynthesizer()
    raise ValueError("VAANISHIELD_TTS_PROVIDER must be 'gnani' or 'gnani_v3_legacy'.")
