import asyncio
import contextlib
import logging
import os
import re
import tempfile
from datetime import datetime, timedelta

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.enums import ChatAction
from aiogram.types import Message
from sqlalchemy.orm import Session

from app.config import settings
from app.database import SessionLocal, Base, engine
from app.models import Lead, ChatMessage, CallRecord
from app.services.openai_service import transcribe_audio, generate_answer
from app.services.learning_service import build_system_prompt, update_learning_profile
from app.services.telephony_service import initiate_ai_call, is_telephony_configured

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)


bot = Bot(token=settings.telegram_bot_token)
dp = Dispatcher()
TYPING_DELAY_SECONDS = 3
CHAT_TEMPERATURE = 0.52
MAX_TG_CHUNK = 3500
NATURAL_PHONE_ASK = (
    "\n\nЩоб передати запит менеджеру, напишіть, будь ласка, ваш номер телефону. "
    "З вами зв'яжеться наш менеджер і проконсультує більш детально за вашим запитом."
)
logger = logging.getLogger("bot_ai.telegram")


def get_or_create_lead(db: Session, message: Message) -> Lead:
    user_id = str(message.from_user.id)
    lead = db.query(Lead).filter(Lead.telegram_user_id == user_id).first()
    if lead:
        if not lead.name and message.from_user and message.from_user.first_name:
            lead.name = message.from_user.first_name.strip()[:64]
            db.add(lead)
            db.commit()
        return lead
    lead = Lead(
        telegram_user_id=user_id,
        username=message.from_user.username,
        name=(message.from_user.first_name.strip()[:64] if message.from_user and message.from_user.first_name else None),
        status="new",
    )
    db.add(lead)
    db.commit()
    db.refresh(lead)
    return lead


def save_message(db: Session, lead_id: int, role: str, content: str, source: str = "text") -> None:
    msg = ChatMessage(lead_id=lead_id, role=role, content=content, source=source)
    db.add(msg)
    db.commit()


def _chat_reply_text(db: Session, lead: Lead, user_text: str) -> str:
    had_assistant_history = _has_assistant_history(db, lead.id)
    recent_dialog = _recent_dialog_context(db, lead.id, limit=12)
    lead_context = (
        f"Лід: ім'я={lead.name or '-'}, телефон={lead.phone or '-'}, "
        f"потреби={lead.needs or '-'}, статус={lead.status}.\n"
        f"Останній контекст діалогу з цим клієнтом:\n{recent_dialog}"
    )
    system_prompt = build_system_prompt(db, lead_context=lead_context)
    raw = generate_answer(system_prompt, user_text, temperature=CHAT_TEMPERATURE)
    return _sanitize_ai_reply(raw, had_assistant_history=had_assistant_history)


async def _typing_pump(chat_id: int, stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            await bot.send_chat_action(chat_id, ChatAction.TYPING)
        except Exception:
            logger.debug("send_chat_action failed", exc_info=True)
        try:
            await asyncio.wait_for(stop.wait(), timeout=4.0)
            return
        except asyncio.TimeoutError:
            continue


async def _chat_reply_with_typing(chat_id: int, db: Session, lead: Lead, user_text: str) -> str:
    try:
        await bot.send_chat_action(chat_id, ChatAction.TYPING)
    except Exception:
        pass
    stop = asyncio.Event()
    pump = asyncio.create_task(_typing_pump(chat_id, stop))
    try:
        return await asyncio.to_thread(_chat_reply_text, db, lead, user_text)
    finally:
        stop.set()
        with contextlib.suppress(Exception):
            await pump


def _eligible_for_auto_call(lead: Lead) -> bool:
    return bool((lead.phone or "").strip())


def _no_recent_call_attempt(db: Session, lead_id: int, seconds: int = 180) -> bool:
    since = datetime.utcnow() - timedelta(seconds=seconds)
    recent = (
        db.query(CallRecord.id)
        .filter(CallRecord.lead_id == lead_id, CallRecord.created_at >= since)
        .first()
    )
    return recent is None


async def _run_auto_ai_call(lead_id: int) -> None:
    await asyncio.sleep(10)
    db = SessionLocal()
    try:
        lead = db.query(Lead).filter(Lead.id == lead_id).first()
        if not lead or not _eligible_for_auto_call(lead):
            logger.warning("Auto-call skipped: insufficient lead data lead_id=%s", lead_id)
            return
        _hydrate_lead_context_before_call(db, lead)
        if not is_telephony_configured():
            logger.warning("Auto-call skipped: telephony not configured lead_id=%s", lead_id)
            return
        if not _no_recent_call_attempt(db, lead_id):
            logger.warning("Auto-call skipped: recent call attempt exists lead_id=%s", lead_id)
            return
        logger.warning("Auto-call starting: lead_id=%s phone=%s", lead.id, lead.phone)
        initiate_ai_call(db, lead)
    except Exception:
        logger.exception("Auto AI call failed for lead_id=%s", lead_id)
    finally:
        db.close()


def _recent_dialog_context(db: Session, lead_id: int, limit: int = 12) -> str:
    rows = (
        db.query(ChatMessage)
        .filter(ChatMessage.lead_id == lead_id)
        .order_by(ChatMessage.created_at.desc())
        .limit(limit)
        .all()
    )
    if not rows:
        return "Немає історії."
    lines: list[str] = []
    for msg in reversed(rows):
        role = "клієнт" if msg.role == "user" else "менеджер"
        lines.append(f"[{role}/{msg.source}] {msg.content}")
    return "\n".join(lines)


def _has_assistant_history(db: Session, lead_id: int) -> bool:
    row = (
        db.query(ChatMessage.id)
        .filter(ChatMessage.lead_id == lead_id, ChatMessage.role == "assistant")
        .first()
    )
    return row is not None


def _hydrate_lead_context_before_call(db: Session, lead: Lead) -> None:
    """Fill minimal call context from chat history if fields are empty."""
    changed = False
    if not (lead.name or "").strip():
        recent_user = (
            db.query(ChatMessage)
            .filter(ChatMessage.lead_id == lead.id, ChatMessage.role == "user")
            .order_by(ChatMessage.created_at.desc())
            .first()
        )
        if recent_user and _looks_like_name(recent_user.content or ""):
            lead.name = recent_user.content.strip().split("\n")[0][:64]
            changed = True
    if not (lead.needs or "").strip():
        candidates = (
            db.query(ChatMessage)
            .filter(ChatMessage.lead_id == lead.id, ChatMessage.role == "user")
            .order_by(ChatMessage.created_at.desc())
            .limit(20)
            .all()
        )
        for msg in candidates:
            text = (msg.content or "").strip()
            if len(text) < 14:
                continue
            if maybe_extract_phone(text):
                continue
            lead.needs = text[:1500]
            changed = True
            break
    if changed:
        db.add(lead)
        db.commit()


def _sanitize_ai_reply(text: str, had_assistant_history: bool) -> str:
    reply = (text or "").strip()
    # Hard ban for robotic phrase about name.
    reply = re.sub(
        r"(?i)напишіть,\s*будь\s*ласка,\s*ваше\s*ім[я'’]\.?",
        "Як я можу до вас звертатися?",
        reply,
    )
    # If dialogue already started, avoid re-greeting at the beginning.
    if had_assistant_history:
        reply = re.sub(
            r"^\s*(привіт|вітаю|доброго дня|добрий день)[,!.\s]*",
            "",
            reply,
            flags=re.IGNORECASE,
        ).strip()
    return reply or "Дякую за повідомлення. Підкажіть, будь ласка, що для вас найважливіше зараз?"


def maybe_extract_phone(text: str) -> str | None:
    """Повертає номер лише у форматі 380XXXXXXXXX (12 цифр) для CRM."""
    source = text or ""
    patterns = [
        r"(?<!\d)\+?\s*380(?:[\s().-]*\d){9}(?!\d)",
        r"(?<!\d)0(?:[\s().-]*\d){9}(?!\d)",
        r"(?<!\d)\d(?:[\s().-]*\d){8}(?!\d)",
    ]
    for pattern in patterns:
        for match in re.finditer(pattern, source):
            digits = "".join(ch for ch in match.group(0) if ch.isdigit())
            if digits.startswith("380") and len(digits) == 12:
                return digits
            if digits.startswith("0") and len(digits) == 10:
                return "38" + digits
            if len(digits) == 9:
                return "380" + digits
    return None


def should_ask_phone(lead: Lead, user_text: str) -> bool:
    if lead.phone:
        return False
    if lead.status == "phone_requested":
        return False
    if not lead.needs:
        return False
    substantial = len(user_text.strip()) >= 20
    return substantial


def _looks_like_name(text: str) -> bool:
    cleaned = text.strip()
    if not cleaned:
        return False
    if maybe_extract_phone(cleaned):
        return False
    if len(cleaned) > 40:
        return False
    # 1-3 words, mostly letters.
    words = [w for w in re.split(r"\s+", cleaned) if w]
    if not (1 <= len(words) <= 3):
        return False
    letters = sum(ch.isalpha() for ch in cleaned)
    return letters >= max(3, len(cleaned) // 2)


def _split_for_telegram(text: str, chunk_size: int = MAX_TG_CHUNK) -> list[str]:
    if len(text) <= chunk_size:
        return [text]
    parts: list[str] = []
    remaining = text
    while remaining:
        if len(remaining) <= chunk_size:
            parts.append(remaining)
            break
        boundary = remaining.rfind("\n", 0, chunk_size)
        if boundary < 200:
            boundary = chunk_size
        parts.append(remaining[:boundary].strip())
        remaining = remaining[boundary:].strip()
    return [p for p in parts if p]


async def send_reply(message: Message, text: str) -> None:
    """Перед відправкою показує статус «пише…» (Telegram typing), перший чанк — після паузи ~3 с."""
    chunks = _split_for_telegram(text or "")
    for idx, chunk in enumerate(chunks):
        try:
            await bot.send_chat_action(chat_id=message.chat.id, action=ChatAction.TYPING)
        except Exception:
            pass
        await asyncio.sleep(TYPING_DELAY_SECONDS if idx == 0 else 1)
        try:
            await message.answer(chunk)
        except Exception as exc:
            logger.exception("Telegram send failed for chat_id=%s: %s", message.chat.id, exc)
            break


@dp.message(CommandStart())
async def cmd_start(message: Message):
    db = SessionLocal()
    try:
        lead = get_or_create_lead(db, message)
        lead.status = "engaged"
        db.add(lead)
        db.commit()
        greeting = (
            "Привіт! Мене звати Софія, я менеджер команди. "
            "Підкажіть, будь ласка, чим можу допомогти саме вам?"
        )
        save_message(db, lead.id, "assistant", greeting)
        await send_reply(message, greeting)
    finally:
        db.close()


@dp.message(F.voice)
async def process_voice(message: Message):
    db = SessionLocal()
    try:
        lead = get_or_create_lead(db, message)
        voice = message.voice
        file = await bot.get_file(voice.file_id)
        with tempfile.NamedTemporaryFile(delete=False, suffix=".ogg") as tmp:
            temp_path = tmp.name
        await bot.download_file(file.file_path, destination=temp_path)

        try:
            transcript = transcribe_audio(temp_path)
        finally:
            if os.path.exists(temp_path):
                os.remove(temp_path)

        save_message(db, lead.id, "user", transcript, source="voice")

        if not lead.name and _looks_like_name(transcript):
            lead.name = transcript.strip().split("\n")[0][:64]
            db.add(lead)
            db.commit()
            reply = f"Дякую, {lead.name}. Розкажіть, будь ласка, коротко, з яким запитом звертаєтесь."
            save_message(db, lead.id, "assistant", reply, source="voice")
            await send_reply(message, reply)
            return

        if not lead.needs and len(transcript.strip()) >= 20 and not maybe_extract_phone(transcript):
            lead.needs = transcript.strip()
        had_phone_before = bool(lead.phone)
        extracted_phone = maybe_extract_phone(transcript)
        if extracted_phone and not lead.phone:
            lead.phone = extracted_phone
            lead.status = "qualified"
            logger.warning("Phone captured from voice: lead_id=%s phone=%s", lead.id, extracted_phone)
        elif not lead.phone and any(ch.isdigit() for ch in transcript):
            logger.warning("Digits detected in voice but phone not parsed: lead_id=%s text=%s", lead.id, transcript)
        db.add(lead)
        db.commit()

        reply = await _chat_reply_with_typing(message.chat.id, db, lead, transcript)
        phone_new_this_message = bool(extracted_phone and not had_phone_before)
        if should_ask_phone(lead, transcript):
            lead.status = "phone_requested"
            db.add(lead)
            db.commit()
            reply += NATURAL_PHONE_ASK
        trigger_auto_call = False
        if phone_new_this_message:
            reply += (
                "\n\nДякую за номер телефону. З вами в скорому часі зв'яжеться наш менеджер "
                "і проконсультує детальніше за вашим запитом."
            )
            trigger_auto_call = True

        save_message(db, lead.id, "assistant", reply, source="voice")
        update_learning_profile(db, f"Голосовий запит: {transcript}. Відповідь: {reply}")
        await send_reply(message, reply)
        if trigger_auto_call:
            logger.warning("Auto-call queued from voice flow: lead_id=%s", lead.id)
            asyncio.create_task(_run_auto_ai_call(lead.id))
    finally:
        db.close()


@dp.message(F.text)
async def process_text_chat(message: Message):
    db = SessionLocal()
    try:
        lead = get_or_create_lead(db, message)
        user_text = message.text.strip()
        save_message(db, lead.id, "user", user_text)

        if not lead.name and _looks_like_name(user_text):
            lead.name = user_text.strip().split("\n")[0][:64]
            db.add(lead)
            db.commit()
            reply = f"Дякую, {lead.name}. Розкажіть, будь ласка, коротко, з яким запитом звертаєтесь."
            save_message(db, lead.id, "assistant", reply)
            await send_reply(message, reply)
            return

        if not lead.needs and len(user_text) >= 20 and not maybe_extract_phone(user_text):
            lead.needs = user_text
        had_phone_before = bool(lead.phone)
        extracted_phone = maybe_extract_phone(user_text)
        if extracted_phone and not lead.phone:
            lead.phone = extracted_phone
            lead.status = "qualified"
            logger.warning("Phone captured from text: lead_id=%s phone=%s", lead.id, extracted_phone)
        elif not lead.phone and any(ch.isdigit() for ch in user_text):
            logger.warning("Digits detected in text but phone not parsed: lead_id=%s text=%s", lead.id, user_text)
        db.add(lead)
        db.commit()

        reply = await _chat_reply_with_typing(message.chat.id, db, lead, user_text)
        phone_new_this_message = bool(extracted_phone and not had_phone_before)
        if should_ask_phone(lead, user_text):
            lead.status = "phone_requested"
            db.add(lead)
            db.commit()
            reply += NATURAL_PHONE_ASK
        trigger_auto_call = False
        if phone_new_this_message:
            reply += (
                "\n\nДякую за номер телефону. З вами в скорому часі зв'яжеться наш менеджер "
                "і проконсультує детальніше за вашим запитом."
            )
            trigger_auto_call = True

        save_message(db, lead.id, "assistant", reply)
        update_learning_profile(db, f"Текст запиту: {user_text}. Відповідь: {reply}")
        await send_reply(message, reply)
        if trigger_auto_call:
            logger.warning("Auto-call queued from text flow: lead_id=%s", lead.id)
            asyncio.create_task(_run_auto_ai_call(lead.id))
    finally:
        db.close()


async def start_bot():
    if not settings.telegram_bot_token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is missing in .env")
    Base.metadata.create_all(bind=engine)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(start_bot())
