import json
import logging
import re
import threading
import time
from typing import Any

import httpx
from sqlalchemy.orm import Session

from app.config import settings
from app.database import SessionLocal
from app.models import CallRecord, ChatMessage, Lead
from app.prompts import (
    CALL_FEEDBACK_SYSTEM_PROMPT,
    CALL_FEEDBACK_USER_PROMPT,
    CRM_OUTPUT_SYSTEM_PROMPT,
    build_crm_output_user_prompt,
)
from app.services.learning_service import update_learning_profile
from app.services.openai_service import generate_answer

logger = logging.getLogger("bot_ai.telephony")

PLACEHOLDER_PREFIX = "Initial prompt:"
TERMINAL_STATUSES = {
    "done",
    "completed",
    "failed",
    "ended",
    "success",
    "successful",
    "no_answer",
    "busy",
    "cancelled",
    "canceled",
    "voicemail",
    "no_webhook_timeout",
}
IN_PROGRESS_STATUSES = {
    "initiated",
    "ringing",
    "in_progress",
    "started",
    "processing",
    "ongoing",
}


def is_placeholder_transcript(text: str | None) -> bool:
    value = (text or "").strip()
    if not value:
        return True
    return value.startswith(PLACEHOLDER_PREFIX)


def format_transcript_items(items: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        role = (item.get("role") or "unknown").strip()
        message = (item.get("message") or item.get("text") or "").strip()
        if not message and item.get("multivoice_message"):
            parts = item["multivoice_message"].get("parts") or []
            message = " ".join(
                str(part.get("text") or "").strip() for part in parts if isinstance(part, dict) and part.get("text")
            ).strip()
        if message:
            label = "Агент" if role == "agent" else "Клієнт" if role == "user" else role
            lines.append(f"{label}: {message}")
    return "\n".join(lines)


def extract_conversation_id(payload: dict[str, Any] | None) -> str:
    if not isinstance(payload, dict):
        return ""
    for key in ("conversation_id", "conversationId"):
        value = payload.get(key)
        if value:
            return str(value).strip()
    for nested_key in ("conversation", "data", "result"):
        nested = payload.get(nested_key)
        if isinstance(nested, dict):
            found = extract_conversation_id(nested)
            if found:
                return found
    return ""


def fetch_elevenlabs_conversation(conversation_id: str) -> dict[str, Any]:
    if not settings.elevenlabs_api_key:
        raise RuntimeError("ELEVENLABS_API_KEY is missing")
    if not conversation_id:
        raise RuntimeError("conversation_id is missing")

    url = f"https://api.elevenlabs.io/v1/convai/conversations/{conversation_id}"
    with httpx.Client(timeout=30.0) as client:
        resp = client.get(url, headers={"xi-api-key": settings.elevenlabs_api_key})
        resp.raise_for_status()
        body = resp.json()
        if not isinstance(body, dict):
            raise RuntimeError("Unexpected ElevenLabs conversation response")
        return body


def find_recent_conversation_for_lead(lead_id: int) -> str:
    """Fallback: find recent agent conversation that carries this lead_id in dynamic vars."""
    if not settings.elevenlabs_api_key or not settings.elevenlabs_agent_id:
        return ""
    url = "https://api.elevenlabs.io/v1/convai/conversations"
    params = {"agent_id": settings.elevenlabs_agent_id, "page_size": 20}
    with httpx.Client(timeout=30.0) as client:
        resp = client.get(url, headers={"xi-api-key": settings.elevenlabs_api_key}, params=params)
        resp.raise_for_status()
        body = resp.json()
    conversations = []
    if isinstance(body, dict):
        conversations = body.get("conversations") or body.get("items") or []
    elif isinstance(body, list):
        conversations = body

    lead_token = str(lead_id)
    fallback_id = ""
    for item in conversations:
        if not isinstance(item, dict):
            continue
        conv_id = str(item.get("conversation_id") or item.get("id") or "").strip()
        if not conv_id:
            continue
        try:
            details = fetch_elevenlabs_conversation(conv_id)
        except Exception:
            continue
        dynamic_vars = (
            (details.get("conversation_initiation_client_data") or {}).get("dynamic_variables")
            or {}
        )
        if str(dynamic_vars.get("lead_id") or "") == lead_token:
            return conv_id
        if not fallback_id and format_transcript_items(details.get("transcript") or []):
            fallback_id = conv_id
    return fallback_id


def _conversation_summary(data: dict[str, Any]) -> str:
    analysis = data.get("analysis") or {}
    if isinstance(analysis, dict):
        for key in ("transcript_summary", "call_summary", "summary"):
            value = analysis.get(key)
            if value:
                return str(value).strip()
    metadata = data.get("metadata") or {}
    if isinstance(metadata, dict):
        for key in ("termination_reason", "error", "detail", "message"):
            value = data.get(key) or metadata.get(key)
            if value:
                return str(value).strip()
    return ""


def _conversation_reason(data: dict[str, Any]) -> str:
    analysis = data.get("analysis") if isinstance(data.get("analysis"), dict) else {}
    metadata = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
    for key in ("call_successful_reason", "termination_reason", "error", "detail", "message"):
        value = data.get(key) or analysis.get(key) or metadata.get(key)
        if value:
            return str(value).strip()
    return ""


def _map_conversation_status(raw_status: str, transcript_text: str) -> str:
    status = (raw_status or "").strip().lower()
    if status in TERMINAL_STATUSES:
        return "completed" if status in {"done", "success", "successful", "ended"} else status
    if transcript_text and status in IN_PROGRESS_STATUSES.union({"", "unknown"}):
        return "completed"
    return status or "initiated"


def apply_elevenlabs_transcript(
    db: Session,
    record: CallRecord,
    data: dict[str, Any],
    *,
    source: str,
    generate_feedback: bool = True,
) -> bool:
    transcript_items = data.get("transcript") or []
    transcript_text = format_transcript_items(transcript_items)
    raw_status = str(data.get("status") or record.status or "initiated")
    mapped_status = _map_conversation_status(raw_status, transcript_text)

    if not transcript_text:
        # Conversation may be finished before transcript is ready — keep waiting.
        if mapped_status in TERMINAL_STATUSES and mapped_status != "no_webhook_timeout":
            record.status = mapped_status
            db.add(record)
            db.commit()
        return False

    summary_text = _conversation_summary(data)
    reason = _conversation_reason(data)
    summary_payload = {
        "summary": summary_text,
        "reason": reason,
        "status": mapped_status,
        "source": source,
    }

    record.status = mapped_status
    record.transcript = transcript_text
    record.summary = json.dumps(summary_payload, ensure_ascii=False)
    conversation_id = extract_conversation_id(data) or (record.call_id or "")
    if conversation_id:
        record.call_id = conversation_id
    db.add(record)
    db.commit()
    db.refresh(record)

    if not generate_feedback:
        return True

    lead = db.query(Lead).filter(Lead.id == record.lead_id).first()
    if not lead:
        return True

    # Avoid duplicate feedback messages for the same call.
    existing_feedback = (
        db.query(ChatMessage)
        .filter(
            ChatMessage.lead_id == lead.id,
            ChatMessage.source == "call_feedback",
            ChatMessage.content.contains(transcript_text[:80]),
        )
        .first()
    )
    if existing_feedback:
        return True

    try:
        feedback_text = generate_answer(
            CALL_FEEDBACK_SYSTEM_PROMPT,
            (
                f"{CALL_FEEDBACK_USER_PROMPT}\n\n"
                f"Контекст ліда з CRM:\n"
                f"- Ім'я: {lead.name or '-'}\n"
                f"- Потреба: {lead.needs or '-'}\n"
                f"- Телефон: {lead.phone or '-'}\n\n"
                f"Транскрипція дзвінка:\n{transcript_text}"
            ),
        )
        db.add(
            ChatMessage(
                lead_id=lead.id,
                role="assistant",
                source="call_feedback",
                content=feedback_text,
            )
        )
        # Structured CRM Output (Doc3 layer 4) for sales handoff.
        try:
            crm_output = generate_answer(
                CRM_OUTPUT_SYSTEM_PROMPT,
                build_crm_output_user_prompt(
                    transcript_text,
                    lead_context=(
                        f"name={lead.name or '-'}; needs={lead.needs or '-'}; "
                        f"phone={lead.phone or '-'}; status={lead.status or '-'}"
                    ),
                ),
            )
            if crm_output.strip():
                # Keep CRM output inside summary JSON for admin visibility.
                try:
                    summary_obj = json.loads(record.summary or "{}")
                except Exception:
                    summary_obj = {"summary": summary_text, "reason": reason, "status": mapped_status}
                summary_obj["crm_output"] = crm_output.strip()
                record.summary = json.dumps(summary_obj, ensure_ascii=False)
                db.add(record)
                # Map high-level status onto the lead when available.
                status_line = ""
                for line in crm_output.splitlines():
                    if line.lower().startswith("lead_status:"):
                        status_line = line.split(":", 1)[-1].strip().upper()
                        break
                status_map = {
                    "DEMO": "demo",
                    "INTERESTED": "interested",
                    "QUALIFIED": "qualified",
                    "SALES": "sales",
                    "HOT": "hot",
                    "FOLLOW_UP": "follow_up",
                    "FOLLOW-UP": "follow_up",
                    "NOT_INTERESTED": "not_interested",
                    "NOT INTERESTED": "not_interested",
                }
                if status_line in status_map:
                    lead.status = status_map[status_line]
                    db.add(lead)
                db.add(
                    ChatMessage(
                        lead_id=lead.id,
                        role="system",
                        source="crm_output",
                        content=crm_output.strip(),
                    )
                )
        except Exception as crm_exc:
            logger.info("CRM output generation failed for record=%s: %s", record.id, crm_exc)

        update_learning_profile(
            db,
            f"ElevenLabs call transcript: {transcript_text}\nSummary: {summary_text}",
        )
        db.commit()
        if settings.telegram_bot_token and str(lead.telegram_user_id or "").isdigit():
            try:
                telegram_url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage"
                httpx.post(
                    telegram_url,
                    json={"chat_id": int(lead.telegram_user_id), "text": feedback_text},
                    timeout=10.0,
                )
            except Exception:
                pass
    except Exception as exc:
        logger.warning("Failed to generate call feedback for record=%s: %s", record.id, exc)

    return True


def resolve_conversation_id(db: Session, record: CallRecord) -> str:
    conversation_id = (record.call_id or "").strip()
    if conversation_id.startswith("conv_"):
        return conversation_id

    # sip_call_id / empty / wrong id — try summary payload first
    summary_raw = (record.summary or "").strip()
    if summary_raw:
        try:
            parsed = json.loads(summary_raw.replace("'", '"')) if summary_raw.startswith("{") else None
        except Exception:
            parsed = None
        if isinstance(parsed, dict):
            found = extract_conversation_id(parsed)
            if found:
                record.call_id = found
                db.add(record)
                db.commit()
                return found

    found = find_recent_conversation_for_lead(record.lead_id)
    if found:
        record.call_id = found
        db.add(record)
        db.commit()
        return found
    return conversation_id


def sync_elevenlabs_call_record(db: Session, record: CallRecord | None) -> bool:
    if not record or record.provider != "elevenlabs_sip":
        return False
    if not is_placeholder_transcript(record.transcript):
        return True

    conversation_id = resolve_conversation_id(db, record)
    if not conversation_id:
        return False

    try:
        data = fetch_elevenlabs_conversation(conversation_id)
    except Exception as exc:
        logger.info(
            "ElevenLabs transcript sync failed for record=%s conversation_id=%s: %s",
            record.id,
            conversation_id,
            exc,
        )
        # Wrong id — try lead fallback once.
        found = find_recent_conversation_for_lead(record.lead_id)
        if not found or found == conversation_id:
            return False
        record.call_id = found
        db.add(record)
        db.commit()
        try:
            data = fetch_elevenlabs_conversation(found)
        except Exception as exc2:
            logger.info("ElevenLabs fallback sync failed record=%s: %s", record.id, exc2)
            return False

    return apply_elevenlabs_transcript(db, record, data, source="api_poll")


def schedule_elevenlabs_transcript_sync(call_record_id: int) -> None:
    if not settings.elevenlabs_api_key:
        return

    def _worker() -> None:
        # Keep trying longer — transcript often appears after call ends.
        delays = [8, 12, 20, 30, 45, 60, 90, 120, 150, 180, 240, 300, 420, 600]
        for delay in delays:
            time.sleep(delay)
            db = SessionLocal()
            try:
                record = db.query(CallRecord).filter(CallRecord.id == call_record_id).first()
                if not record:
                    return
                if record.provider != "elevenlabs_sip":
                    return
                if not is_placeholder_transcript(record.transcript):
                    return
                synced = sync_elevenlabs_call_record(db, record)
                if synced:
                    logger.info("ElevenLabs transcript synced for call_record_id=%s", call_record_id)
                    return
            finally:
                db.close()

    threading.Thread(target=_worker, name=f"el-transcript-{call_record_id}", daemon=True).start()


def parse_call_summary(summary_text: str | None) -> str:
    raw = (summary_text or "").strip()
    if not raw:
        return ""
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            for key in ("summary", "reason", "status"):
                value = parsed.get(key)
                if not value:
                    continue
                text = str(value).strip()
                if text.lower() in {
                    "event update",
                    "auto-closed stale call",
                    "no final webhook status received in time",
                    "no_webhook_timeout",
                }:
                    continue
                return text
    except Exception:
        pass
    if raw.startswith("{") and raw.endswith("}"):
        match = re.search(r"'summary'\s*:\s*'([^']+)'", raw)
        if match:
            return match.group(1).strip()
    return raw[:240]


def friendly_call_status(record: CallRecord | None) -> str:
    if not record:
        return "—"
    status = (record.status or "").lower()
    if status in {"completed", "done", "success", "successful"}:
        return "Завершено"
    if status in {"initiated", "ringing", "in_progress", "started", "processing"}:
        return "У процесі"
    if status == "no_webhook_timeout":
        if is_placeholder_transcript(record.transcript):
            return "Очікуємо транскрипцію"
        return "Завершено"
    if status in {"failed", "busy", "no_answer", "cancelled", "canceled"}:
        return "Не завершено"
    return record.status or "—"
