import os
import uuid
import httpx
from app.config import settings


def synthesize_to_file(text: str, output_dir: str = "media") -> str:
    if not settings.elevenlabs_api_key or not settings.elevenlabs_voice_id:
        raise RuntimeError("ELEVENLABS_API_KEY or ELEVENLABS_VOICE_ID is missing")

    os.makedirs(output_dir, exist_ok=True)
    file_name = f"{uuid.uuid4().hex}.mp3"
    file_path = os.path.join(output_dir, file_name)

    url = f"https://api.elevenlabs.io/v1/text-to-speech/{settings.elevenlabs_voice_id}"
    headers = {
        "xi-api-key": settings.elevenlabs_api_key,
        "Content-Type": "application/json",
    }
    payload = {
        "text": text,
        "model_id": settings.elevenlabs_model_id,
        "voice_settings": {
            "stability": 0.4,
            "similarity_boost": 0.75,
            "style": 0.2,
            "use_speaker_boost": True,
        },
    }

    with httpx.Client(timeout=60.0) as client:
        response = client.post(url, headers=headers, json=payload)
        response.raise_for_status()
        with open(file_path, "wb") as f:
            f.write(response.content)

    return file_path
