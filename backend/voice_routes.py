


import tempfile
import uuid
from pathlib import Path

from fastapi import APIRouter, UploadFile, File, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from services.gnani_speech import (
    transcribe_audio,
    synthesize_speech,
)

router = APIRouter(prefix="/api/voice", tags=["Gnani Voice"])

MAX_AUDIO_SIZE = 15 * 1024 * 1024


class SpeechRequest(BaseModel):
    text: str = Field(min_length=1, max_length=3000)
    language: str = "kn-IN"
    voice: str = "Shivani"


@router.post("/transcribe")
async def voice_transcription(audio: UploadFile = File(...)):
    """Receive customer audio and transcribe using Gnani Prisma."""

    content = await audio.read(MAX_AUDIO_SIZE + 1)

    if not content:
        raise HTTPException(400, "Empty audio file.")

    if len(content) > MAX_AUDIO_SIZE:
        raise HTTPException(413, "Audio file exceeds 15 MB.")

    suffix = Path(audio.filename or "recording.wav").suffix

    if not suffix or len(suffix) > 10:
        suffix = ".wav"

    try:
        with tempfile.NamedTemporaryFile(
            suffix=suffix,
            delete=False,
        ) as temp:
            temp.write(content)
            temp_path = Path(temp.name)

        try:
            result = transcribe_audio(temp_path)
        finally:
            temp_path.unlink(missing_ok=True)

        return {
            "success": True,
            "transcript": result["transcript"],
            "provider": result["provider"],
            "model": result["model"],
        }

    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Prisma transcription failed: {exc}",
        )


@router.post("/speak")
def generate_speech(request: SpeechRequest):
    """Generate spoken response using Gnani Timbre."""

    filename = f"response_{uuid.uuid4().hex}.wav"

    try:
        result = synthesize_speech(
            request.text,
            language=request.language,
            voice=request.voice,
            filename=filename,
        )

        return FileResponse(
            path=result["audio_path"],
            media_type="audio/wav",
            filename="vaanishield_response.wav",
        )

    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Timbre speech generation failed: {exc}",
        )
