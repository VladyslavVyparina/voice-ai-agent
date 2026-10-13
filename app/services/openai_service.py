from openai import OpenAI
from app.config import settings
import httpx
import tempfile
import os

client = OpenAI(api_key=settings.openai_api_key) if settings.openai_api_key else None


def transcribe_audio(file_path: str) -> str:
    if client is None:
        return "OpenAI API key is missing. Cannot transcribe audio."

    with open(file_path, "rb") as audio:
        result = client.audio.transcriptions.create(
            model="whisper-1",
            file=audio,
            language="uk",
        )
    return result.text


def transcribe_audio_url(audio_url: str) -> str:
    if client is None:
        return "OpenAI API key is missing. Cannot transcribe audio."

    with httpx.Client(timeout=60.0) as http:
        response = http.get(audio_url)
        response.raise_for_status()
        with tempfile.NamedTemporaryFile(delete=False, suffix=".mp3") as tmp:
            tmp.write(response.content)
            temp_path = tmp.name
    try:
        return transcribe_audio(temp_path)
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)


def generate_answer(system_prompt: str, user_prompt: str, temperature: float = 0.35) -> str:
    if client is None:
        return (
            "Наразі AI-відповідь недоступна, бо не налаштований OPENAI_API_KEY. "
            "Я вже зберіг ваш запит і менеджер зв'яжеться з вами."
        )

    completion = client.chat.completions.create(
        model=settings.chat_model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        temperature=temperature,
    )
    return completion.choices[0].message.content or "Дякую, отримав ваш запит."
