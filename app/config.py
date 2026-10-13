import os
from dataclasses import dataclass
from dotenv import load_dotenv

load_dotenv()


@dataclass
class Settings:
    telegram_bot_token: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    openai_api_key: str = os.getenv("OPENAI_API_KEY", "")
    chat_model: str = os.getenv("CHAT_MODEL", "gpt-4o-mini")

    app_host: str = os.getenv("APP_HOST", "0.0.0.0")
    app_port: int = int(os.getenv("APP_PORT", "8000"))
    database_url: str = os.getenv("DATABASE_URL", "sqlite:///./crm.db")

    zadarma_key: str = os.getenv("ZADARMA_KEY", "")
    zadarma_secret: str = os.getenv("ZADARMA_SECRET", "")
    zadarma_from: str = os.getenv("ZADARMA_FROM", "")
    zadarma_scenario_id: str = os.getenv("ZADARMA_SCENARIO_ID", "")
    zadarma_caller_id: str = os.getenv("ZADARMA_CALLER_ID", "")
    zadarma_sip: str = os.getenv("ZADARMA_SIP", "")
    elevenlabs_api_key: str = os.getenv("ELEVENLABS_API_KEY", "")
    elevenlabs_voice_id: str = os.getenv("ELEVENLABS_VOICE_ID", "")
    elevenlabs_model_id: str = os.getenv("ELEVENLABS_MODEL_ID", "eleven_multilingual_v2")
    elevenlabs_agent_id: str = os.getenv("ELEVENLABS_AGENT_ID", "")
    elevenlabs_agent_phone_number_id: str = os.getenv("ELEVENLABS_AGENT_PHONE_NUMBER_ID", "")
    elevenlabs_webhook_secret: str = os.getenv("ELEVENLABS_WEBHOOK_SECRET", "")
    telephony_provider: str = os.getenv("TELEPHONY_PROVIDER", "auto")
    public_base_url: str = os.getenv("PUBLIC_BASE_URL", "http://localhost:8000")

    admin_token: str = os.getenv("ADMIN_TOKEN", "change_this_token")
    admin_email: str = os.getenv("ADMIN_EMAIL", "admin@example.com")
    admin_password: str = os.getenv("ADMIN_PASSWORD", "")

    def __post_init__(self) -> None:
        # Avoid double slashes when building webhook URLs (e.g. PUBLIC_BASE_URL ends with /)
        object.__setattr__(self, "public_base_url", self.public_base_url.rstrip("/"))


settings = Settings()
