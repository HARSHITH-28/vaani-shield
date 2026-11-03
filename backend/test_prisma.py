
import os
import requests
from dotenv import load_dotenv

load_dotenv()

API_KEY = os.getenv("GNANI_API_KEY")

URL = "https://api.vachana.ai/stt/v3"
AUDIO_PATH = "data/audio/kannada_test.ogg"

if not API_KEY:
    raise ValueError("Gnani API key not found in .env")

headers = {
    "X-API-Key-ID": API_KEY
}

data = {
    "model": "prisma-v3.0",
    "language_code": "indic-auto",
    "multi_lang_codes": "en-IN,hi-IN,kn-IN",
    "format": "transcribe"
}

print("Connecting to Gnani Prisma V3...")

with open(AUDIO_PATH, "rb") as audio:
    files = {
        "audio_file": ("test.wav.ogg", audio, "audio/ogg")
    }

    response = requests.post(
        URL,
        headers=headers,
        data=data,
        files=files,
        timeout=60
    )

print("HTTP Status:", response.status_code)
print("Response:")

try:
    result = response.json()
    print(result)

    if result.get("success"):
        print("\nTRANSCRIPTION:")
        print(result.get("transcript", "No transcript returned."))

except ValueError:
    print(response.text)
