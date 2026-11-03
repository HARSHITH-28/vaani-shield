import os
import requests
from dotenv import load_dotenv

load_dotenv()

API_KEY = os.getenv("GNANI_API_KEY")
URL = "https://api.vachana.ai/api/v1/tts/inference"

payload = {
    "text": "ಎಚ್ಚರಿಕೆ! ನಿಮ್ಮ OTP ಅನ್ನು ಯಾರೊಂದಿಗೂ ಹಂಚಿಕೊಳ್ಳಬೇಡಿ. ಇದು ಮೋಸದ ಕರೆ ಆಗಿರಬಹುದು.",
    "voice": "Shivani",
    "model": "timbre-v3.0",
    "language": "kn-IN",
    "speed": 1.0,
    "audio_config": {
        "sample_rate": 48000,
        "num_channels": 1,
        "sample_width": 2,
        "encoding": "linear_pcm",
        "container": "wav"
    }
}

headers = {
    "X-API-Key-ID": API_KEY,
    "Content-Type": "application/json"
}

print("Connecting to Gnani Timbre v3.0...")

response = requests.post(
    URL,
    headers=headers,
    json=payload,
    timeout=60
)

print("HTTP Status:", response.status_code)

if response.status_code == 200:
    os.makedirs("data/audio", exist_ok=True)

    with open("data/audio/warning_kn.wav", "wb") as audio:
        audio.write(response.content)

    print("SUCCESS! Audio saved to data/audio/warning_kn.wav")
else:
    print(response.text)