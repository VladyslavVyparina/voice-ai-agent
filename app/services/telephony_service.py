from sqlalchemy.orm import Session
import json
from app.models import Lead, CallRecord, ChatMessage
from app.services.elevenlabs_service import synthesize_to_file
from app.services.zadarma_service import start_callback_call
from app.services.elevenlabs_convai_service import start_outbound_sip_call
from app.services.call_transcript_service import (
    extract_conversation_id,
    schedule_elevenlabs_transcript_sync,
)
from app.prompts import CALL_BRIEFING_USER_PROMPT, VALERIY_CALL_BRIEFING
from app.config import settings

FIXED_FIRST_MESSAGE = "Вітаю, мене звати Валерій, я менеджер компанії Pulse AI. Зручно зараз говорити?"


def _public_lead_needs(raw: str) -> str:
    """Needs text safe to speak aloud (no demo/service labels)."""
    text = (raw or "").strip()
    if not text:
        return ""
    lowered = text.casefold()
    blocked = (
        "демо",
        "demo",
        "тестов",
        "website",
        "widget",
        "вхідний дзвінок",
        "запит з сайту",
    )
    if any(m in lowered for m in blocked):
        return ""
    return text


def _build_call_briefing(lead: Lead, lead_context: str) -> str:
    """Build Call Briefing for Valeriy outbound call (Doc3 layer 3)."""
    briefing = VALERIY_CALL_BRIEFING or CALL_BRIEFING_USER_PROMPT
    safe_needs = _public_lead_needs(lead.needs or "")
    return (
        f"{briefing}\n\n"
        f"Контекст цього ліда:\n{lead_context}\n\n"
        f"Ім'я: {lead.name or '—'}\n"
        f"Запит: {safe_needs or '—'}\n"
        "Перша фраза вже задана системою. Не змінюй її. "
        "Не кажи «демо» і не кажи «консультувати»."
    )


def _is_elevenlabs_configured() -> bool:
    return bool(
        settings.elevenlabs_api_key
        and settings.elevenlabs_agent_id
        and settings.elevenlabs_agent_phone_number_id
    )


def _is_zadarma_configured() -> bool:
    return bool(settings.zadarma_key and settings.zadarma_secret)


def _resolve_provider() -> str:
    raw = (settings.telephony_provider or "auto").strip().lower()
    eleven_aliases = {"elevenlabs", "elevenlabs_sip", "elevenlabs-sip"}
    zadarma_aliases = {"zadarma"}
    if raw in eleven_aliases:
        if _is_elevenlabs_configured():
            return "elevenlabs_sip"
        if _is_zadarma_configured():
            return "zadarma"
        raise RuntimeError("TELEPHONY_PROVIDER=elevenlabs_sip, but ElevenLabs credentials are incomplete")
    if raw in zadarma_aliases:
        if _is_zadarma_configured():
            return "zadarma"
        if _is_elevenlabs_configured():
            return "elevenlabs_sip"
        raise RuntimeError("TELEPHONY_PROVIDER=zadarma, but Zadarma credentials are incomplete")
    if _is_elevenlabs_configured():
        return "elevenlabs_sip"
    if _is_zadarma_configured():
        return "zadarma"
    raise RuntimeError("No telephony provider configured (missing ElevenLabs and Zadarma credentials)")


def is_telephony_configured() -> bool:
    try:
        _resolve_provider()
        return True
    except Exception:
        return False


def _normalize_phone_380(raw: str) -> str:
    digits = "".join(ch for ch in (raw or "") if ch.isdigit())
    if digits.startswith("380") and len(digits) == 12:
        return digits
    if digits.startswith("0") and len(digits) == 10:
        return "38" + digits
    return ""


def _phone_e164(raw: str) -> str:
    phone_380 = _normalize_phone_380(raw)
    if not phone_380:
        return ""
    return f"+{phone_380}"


def _fallback_call_script(lead: Lead) -> str:
    name = (lead.name or "").strip()
    if name:
        return f"{name}, добрий день. {FIXED_FIRST_MESSAGE}"
    return f"Добрий день. {FIXED_FIRST_MESSAGE}"


def _lead_chat_context(db: Session, lead_id: int, limit: int = 12) -> str:
    msgs = (
        db.query(ChatMessage)
        .filter(ChatMessage.lead_id == lead_id)
        .order_by(ChatMessage.created_at.desc())
        .limit(limit)
        .all()
    )
    if not msgs:
        return "Немає історії чату."
    rows = []
    for m in reversed(msgs):
        rows.append(f"[{m.role}/{m.source}] {m.content}")
    return "\n".join(rows)


def initiate_ai_call(db: Session, lead: Lead) -> CallRecord:
    per_lead_chat = _lead_chat_context(db, lead.id)
    safe_needs = _public_lead_needs(lead.needs or "")
    lead_context = (
        f"Лід: ім'я={lead.name or '-'}, телефон={lead.phone or '-'}, "
        f"потреба={safe_needs or '-'}, статус={lead.status}.\n"
        f"Історія чату цього ліда:\n{per_lead_chat}\n"
        "Використовуй лише ці дані по цьому лідові. Не змішуй контекст інших клієнтів. "
        "Не кажи вголос слова «демо» або «консультувати»."
    )
    briefing_prompt = _build_call_briefing(lead, lead_context)
    # Fixed opening — never generate "консультувати" / "демо" via LLM.
    first_prompt = FIXED_FIRST_MESSAGE
    note = ""
    provider = _resolve_provider()

    def _call_with_elevenlabs() -> CallRecord:
        phone_e164 = _phone_e164(lead.phone or "")
        if not phone_e164:
            raise RuntimeError(
                f"Invalid lead phone for ElevenLabs SIP: '{lead.phone}'. "
                "Expected 380XXXXXXXXX or 0XXXXXXXXX in CRM."
            )
        lead.phone = phone_e164[1:]
        db.add(lead)
        db.commit()
        eleven_response = start_outbound_sip_call(
            to_number_e164=phone_e164,
            lead_id=lead.id,
            lead_context=lead_context,
            first_message=first_prompt,
            lead_name=(lead.name or "").strip(),
            lead_needs=_public_lead_needs(lead.needs or ""),
            call_briefing=briefing_prompt,
        )
        conversation_id = extract_conversation_id(eleven_response) or str(
            eleven_response.get("conversation_id", "") or eleven_response.get("sip_call_id", "") or ""
        )
        return CallRecord(
            lead_id=lead.id,
            provider="elevenlabs_sip",
            status="initiated",
            call_id=conversation_id,
            transcript=f"Initial prompt: {first_prompt}{note}",
            summary=json.dumps(eleven_response, ensure_ascii=False, default=str),
        )

    def _call_with_zadarma() -> CallRecord:
        audio_file = synthesize_to_file(first_prompt)
        zadarma_response = start_callback_call(lead.phone or "", first_prompt, lead.id)
        public_audio_url = (
            f"{settings.public_base_url}/{audio_file}".replace("//media", "/media")
            if audio_file
            else ""
        )
        return CallRecord(
            lead_id=lead.id,
            provider="zadarma",
            status="initiated",
            call_id=str(zadarma_response.get("id", "")),
            transcript=(
                f"Initial prompt: {first_prompt}{note}\n"
                f"Audio file: {audio_file}\n"
                f"Audio URL: {public_audio_url}"
            ),
            summary=str(zadarma_response),
        )

    errors: list[str] = []
    try:
        record = _call_with_elevenlabs() if provider == "elevenlabs_sip" else _call_with_zadarma()
    except Exception as primary_exc:
        errors.append(f"{provider}: {primary_exc}")
        fallback = "zadarma" if provider == "elevenlabs_sip" else "elevenlabs_sip"
        can_fallback = (fallback == "zadarma" and _is_zadarma_configured()) or (
            fallback == "elevenlabs_sip" and _is_elevenlabs_configured()
        )
        if not can_fallback:
            raise RuntimeError("; ".join(errors)) from primary_exc
        try:
            record = _call_with_zadarma() if fallback == "zadarma" else _call_with_elevenlabs()
            record.summary = f"{record.summary}\n[fallback_from={provider}]"
        except Exception as fallback_exc:
            errors.append(f"{fallback}: {fallback_exc}")
            raise RuntimeError("; ".join(errors)) from fallback_exc

    db.add(record)
    db.commit()
    db.refresh(record)
    if record.provider == "elevenlabs_sip" and (record.call_id or record.lead_id):
        schedule_elevenlabs_transcript_sync(record.id)
    return record
