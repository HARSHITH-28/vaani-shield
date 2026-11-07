"""Provider-neutral speech and orchestration interfaces."""

from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from backend.api_models import SpeechOutput
from services.principal import CustomerPrincipal


class AssistantServiceError(Exception):
    """Base class for safe orchestration errors."""

    def __init__(self, message: str = "", *, code: str = "ASSISTANT_SERVICE_ERROR"):
        super().__init__(message)
        self.code = code


class InputProcessingUnavailable(AssistantServiceError):
    pass


class OutputProcessingUnavailable(AssistantServiceError):
    pass


class AudioUploadUnavailable(InputProcessingUnavailable):
    pass


@dataclass(frozen=True)
class Transcription:
    text: str
    languages: tuple[str, ...] = ()
    is_code_mixed: bool = False


class SpeechTranscriber(Protocol):
    def transcribe(
        self, principal: CustomerPrincipal, audio_upload_id: UUID, language_hint: str
    ) -> Transcription: ...


class SpeechSynthesizer(Protocol):
    def synthesize(
        self, principal: CustomerPrincipal, text: str, language: str
    ) -> SpeechOutput: ...
