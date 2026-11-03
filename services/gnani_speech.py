
import os
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()

API_KEY = os.getenv("GNANI_API_KEY")

PRISMA_URL = "https://api.vachana.ai/stt/v3"
TIMBRE_URL = "https://api.vachana.ai/api/v1/tts/inference"

AUDIO_DIR = Path(__file__).resolve().parent.parent / "data" / "audio"


def _get_headers(json_content=False):
    if not API_KEY:
        raise RuntimeError("GNANI_API_KEY is missing from .env")

    headers = {"X-API-Key-ID": API_KEY}

    if json_content:
        headers["Content-Type"] = "application/json"

    return headers


def transcribe_audio(audio_path, language_code="indic-auto"):
    """Transcribe an audio file using Gnani Prisma v3."""

    path = Path(audio_path)

    if not path.is_file():
        raise FileNotFoundError(f"Audio file not found: {path}")

    data = {
        "model": "prisma-v3.0",
        "language_code": language_code,
        "multi_lang_codes": "en-IN,hi-IN,kn-IN",
        "format": "transcribe",
    }

    with path.open("rb") as audio:
        response = requests.post(
            PRISMA_URL,
            headers=_get_headers(),
            data=data,
            files={
                "audio_file": (
                    path.name,
                    audio,
                    "application/octet-stream",
                )
            },
            timeout=60,
        )

    response.raise_for_status()

    result = response.json()

    if not result.get("success"):
        raise RuntimeError(f"Prisma transcription failed: {result}")

    transcript = result.get("transcript")

    if not transcript:
        raise RuntimeError("Prisma returned an empty transcript.")

    return {
        "transcript": transcript,
        "provider": "Gnani Prisma",
        "model": "prisma-v3.0",
        "raw_response": result,
    }


def synthesize_speech(
    text,
    language="kn-IN",
    voice="Shivani",
    filename="vaanishield_response.wav",
):
    """Generate speech using Gnani Timbre v3."""

    if not text or not text.strip():
        raise ValueError("Speech text cannot be empty.")

    payload = {
        "text": text,
        "voice": voice,
        "model": "timbre-v3.0",
        "language": language,
        "speed": 1.0,
        "audio_config": {
            "sample_rate": 48000,
            "num_channels": 1,
            "sample_width": 2,
            "encoding": "linear_pcm",
            "container": "wav",
        },
    }

    response = requests.post(
        TIMBRE_URL,
        headers=_get_headers(json_content=True),
        json=payload,
        timeout=60,
    )

    response.raise_for_status()

    if not response.content:
        raise RuntimeError("Timbre returned an empty audio response.")

    AUDIO_DIR.mkdir(parents=True, exist_ok=True)

    output_path = AUDIO_DIR / filename

    if output_path.parent != AUDIO_DIR:
        raise ValueError("Invalid audio filename.")

    output_path.write_bytes(response.content)

    return {
        "audio_path": str(output_path),
        "provider": "Gnani Timbre",
        "model": "timbre-v3.0",
        "language": language,
        "voice": voice,
    }
