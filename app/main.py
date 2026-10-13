import asyncio
import httpx
import json
import logging
from fastapi import FastAPI, Depends
from fastapi import Request
from pydantic import BaseModel
from sqlalchemy.orm import Session
from fastapi.staticfiles import StaticFiles

from app.config import settings
from app.database import Base, engine, get_db, SessionLocal
from app.models import Lead, CallRecord, ChatMessage
from app.admin import router as admin_router
from app.services.learning_service import update_learning_profile, run_daily_learning_update
from app.services.openai_service import generate_answer, transcribe_audio_url
from app.services.call_transcript_service import apply_elevenlabs_transcript
from app.prompts import CALL_FEEDBACK_SYSTEM_PROMPT, CALL_FEEDBACK_USER_PROMPT

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)

app = FastAPI(title="Business AI Agent CRM")
Base.metadata.create_all(bind=engine)
logger = logging.getLogger("bot_ai.telephony")

app.include_router(admin_router, prefix="/admin", tags=["admin"])
app.mount("/media", StaticFiles(directory="media"), name="media")


async def _daily_learning_worker():
    while True:
        await asyncio.sleep(60 * 60 * 24)
        db = SessionLocal()
        try:
            run_daily_learning_update(db)
        finally:
            db.close()


@app.get("/")
def root():
    return {
        "service": "Business AI Agent CRM",
        "status": "ok",
        "paths": {
            "health": "/health",
            "admin": "/admin",
            "admin_login": "/admin/login",
            "ai_knowledge": "/admin/ai-knowledge",
            "api_docs": "/docs",
            "zadarma_webhook": "/telephony/zadarma/webhook",
            "elevenlabs_webhook": "/telephony/elevenlabs/webhook",
        },
        "hint_ua": "Адмін-панель: /admin/login (логін і пароль) або через x-admin-token. Документація API: /docs.",
    }


@app.get("/health")
def health():
    return {"ok": True}


def _run_migrations():
    """Apply lightweight schema migrations for existing databases."""
    from sqlalchemy import text, inspect
    with engine.connect() as conn:
        insp = inspect(engine)
        leads_cols = [c["name"] for c in insp.get_columns("leads")]
        if "source" not in leads_cols:
            conn.execute(text("ALTER TABLE leads ADD COLUMN source VARCHAR(64) DEFAULT 'telegram'"))
            conn.commit()


@app.on_event("startup")
async def startup_events():
    _run_migrations()
    asyncio.create_task(_daily_learning_worker())


@app.post("/jobs/daily-learning")
def trigger_daily_learning():
    db = SessionLocal()
    try:
        run_daily_learning_update(db)
        return {"ok": True, "message": "Daily learning update completed"}
    finally:
        db.close()


class WebsiteDemoCallRequest(BaseModel):
    phone: str
    name: str = ""


@app.post("/api/website-demo-call")
def website_demo_call(payload: WebsiteDemoCallRequest, db: Session = Depends(get_db)):
    """
    Public endpoint called by the website landing page.
    Creates a lead with source='website_demo' and immediately initiates an AI call.
    """
    from app.services.telephony_service import initiate_ai_call

    phone_raw = (payload.phone or "").strip()
    name_raw = (payload.name or "").strip()

    if not phone_raw:
        return {"ok": False, "error": "phone_required"}

    digits = "".join(ch for ch in phone_raw if ch.isdigit())
    if digits.startswith("380") and len(digits) == 12:
        phone_normalized = digits
    elif digits.startswith("0") and len(digits) == 10:
        phone_normalized = "38" + digits
    elif len(digits) == 12:
        phone_normalized = digits
    else:
        return {"ok": False, "error": "invalid_phone"}

    existing = db.query(Lead).filter(Lead.phone == phone_normalized, Lead.source == "website_demo").first()
    if existing:
        try:
            record = initiate_ai_call(db, existing)
            return {"ok": True, "lead_id": existing.id, "call_record_id": record.id, "reused": True, "call_started": True}
        except Exception as exc:
            logger.error("website_demo_call re-call failed: %s", exc)
            return {"ok": True, "lead_id": existing.id, "call_started": False, "warning": "call_failed"}

    lead = Lead(
        telegram_user_id="website_demo",
        username=None,
        name=name_raw or None,
        phone=phone_normalized,
        needs="Запит з сайту",
        status="demo",
        source="website_demo",
    )
    db.add(lead)
    db.commit()
    db.refresh(lead)

    try:
        record = initiate_ai_call(db, lead)
    except Exception as exc:
        logger.error("website_demo_call initiate failed: %s", exc)
        return {"ok": True, "lead_id": lead.id, "call_started": False, "warning": "call_failed"}

    return {"ok": True, "lead_id": lead.id, "call_record_id": record.id, "call_started": True}


class ZadarmaWebhookPayload(BaseModel):
    lead_id: int
    call_id: str | None = None
    status: str = "completed"
    transcript: str = ""
    summary: str = ""
    recording_url: str | None = None


@app.post("/telephony/zadarma/webhook")
def zadarma_webhook(payload: ZadarmaWebhookPayload, db: Session = Depends(get_db)):
    lead = db.query(Lead).filter(Lead.id == payload.lead_id).first()
    if not lead:
        return {"ok": False, "error": "lead_not_found"}

    transcript = payload.transcript
    if not transcript and payload.recording_url:
        try:
            transcript = transcribe_audio_url(payload.recording_url)
        except Exception:
            transcript = ""

    summary_payload = {"summary": payload.summary or ""}
    if payload.status and payload.status.lower() not in {"completed", "done", "success"}:
        summary_payload["reason"] = payload.summary or "Call not completed"
    record = CallRecord(
        lead_id=lead.id,
        provider="zadarma",
        status=payload.status,
        call_id=payload.call_id,
        transcript=transcript,
        summary=json.dumps(summary_payload, ensure_ascii=False),
    )
    db.add(record)
    db.commit()
    db.refresh(record)

    if transcript:
        feedback_text = generate_answer(
            CALL_FEEDBACK_SYSTEM_PROMPT,
            (
                f"{CALL_FEEDBACK_USER_PROMPT}\n\n"
                f"Контекст ліда з CRM:\n"
                f"- Ім'я: {lead.name or '-'}\n"
                f"- Потреба: {lead.needs or '-'}\n"
                f"- Телефон: {lead.phone or '-'}\n\n"
                f"Транскрипція дзвінка:\n{transcript}"
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
        update_learning_profile(db, f"Транскрипція дзвінка: {transcript}\nSummary: {payload.summary}")
        db.commit()

        if settings.telegram_bot_token:
            try:
                telegram_url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage"
                httpx.post(
                    telegram_url,
                    json={
                        "chat_id": int(lead.telegram_user_id),
                        "text": feedback_text,
                    },
                    timeout=10.0,
                )
            except Exception:
                # Non-fatal for webhook flow.
                pass

    return {"ok": True, "call_record_id": record.id}


@app.post("/telephony/elevenlabs/webhook")
async def elevenlabs_webhook(request: Request, db: Session = Depends(get_db)):
    payload = await request.json()
    event_type = payload.get("type", "")
    data = payload.get("data", {}) or {}
    dynamic_vars = (
        data.get("conversation_initiation_client_data", {}).get("dynamic_variables", {})
        or payload.get("conversation_initiation_client_data", {}).get("dynamic_variables", {})
        or {}
    )
    conversation_id = (
        data.get("conversation_id")
        or payload.get("conversation_id")
        or data.get("sip_call_id")
        or payload.get("sip_call_id")
        or data.get("call_id")
        or ""
    )
    lead_id_raw = dynamic_vars.get("lead_id")
    lead_id: int | None = None
    if lead_id_raw:
        try:
            lead_id = int(str(lead_id_raw))
        except ValueError:
            lead_id = None
    logger.info(
        "ElevenLabs webhook received: type=%s conversation_id=%s lead_id=%s status=%s",
        event_type,
        conversation_id,
        lead_id,
        data.get("status"),
    )

    def _find_record() -> CallRecord | None:
        if conversation_id:
            rec = (
                db.query(CallRecord)
                .filter(CallRecord.provider == "elevenlabs_sip", CallRecord.call_id == conversation_id)
                .order_by(CallRecord.created_at.desc())
                .first()
            )
            if rec:
                return rec
        if lead_id:
            return (
                db.query(CallRecord)
                .filter(CallRecord.provider == "elevenlabs_sip", CallRecord.lead_id == lead_id)
                .order_by(CallRecord.created_at.desc())
                .first()
            )
        return None

    # Track non-transcription events too, because they may carry failure reason/status.
    if event_type != "post_call_transcription":
        existing_record = _find_record()
        if not existing_record:
            logger.warning(
                "ElevenLabs webhook not mapped: type=%s conversation_id=%s lead_id=%s",
                event_type,
                conversation_id,
                lead_id,
            )
            return {"ok": True, "ignored": True, "type": event_type, "mapped": False}
        reason = (
            data.get("termination_reason")
            or data.get("error")
            or data.get("detail")
            or data.get("message")
            or ""
        )
        status = data.get("status") or event_type
        existing_record.status = status
        if conversation_id and not existing_record.call_id:
            existing_record.call_id = conversation_id
        existing_record.summary = json.dumps(
            {
                "summary": "Event update",
                "reason": reason,
                "status": status,
                "event_type": event_type,
            },
            ensure_ascii=False,
        )
        db.add(existing_record)
        db.commit()
        logger.info(
            "ElevenLabs event mapped: call_record_id=%s status=%s reason=%s",
            existing_record.id,
            status,
            reason,
        )
        return {"ok": True, "tracked": True, "type": event_type}

    # Fallback mapping: locate initiated call by conversation_id.
    existing_record = _find_record()
    if existing_record and not lead_id:
        lead_id = existing_record.lead_id

    # If no lead_id, create a new lead for website widget calls
    if not lead_id:
        logger.info(
            "ElevenLabs webhook: no lead_id, creating new lead for widget call conversation_id=%s",
            conversation_id,
        )
        # Extract any available info from the webhook data
        metadata = data.get("metadata", {}) or {}
        analysis = data.get("analysis", {}) or {}
        
        # Try to get caller info from metadata
        caller_phone = metadata.get("caller_phone_number") or metadata.get("phone") or ""
        
        # Create new lead for website call
        new_lead = Lead(
            name="Вхідний дзвінок з сайту",
            phone=caller_phone if caller_phone else None,
            status="website_call",
            needs=f"Вхідний дзвінок через widget (conv: {conversation_id[:20] if conversation_id else 'unknown'}...)",
        )
        db.add(new_lead)
        db.commit()
        db.refresh(new_lead)
        lead_id = new_lead.id
        lead = new_lead
        logger.info("Created new lead for widget call: lead_id=%s", lead_id)
    else:
        lead = db.query(Lead).filter(Lead.id == lead_id).first()
        if not lead:
            logger.warning("ElevenLabs transcription webhook lead not found: lead_id=%s", lead_id)
            return {"ok": False, "error": "lead_not_found"}

    transcript_items = data.get("transcript", []) or []
    analysis = data.get("analysis", {}) or {}
    reason = (
        data.get("termination_reason")
        or data.get("error")
        or data.get("detail")
        or data.get("message")
        or analysis.get("call_successful_reason")
        or ""
    )
    payload_for_apply = {
        **data,
        "status": data.get("status", "completed"),
        "transcript": transcript_items,
        "analysis": analysis,
        "termination_reason": reason,
    }

    if existing_record:
        if conversation_id and not existing_record.call_id:
            existing_record.call_id = conversation_id
            db.add(existing_record)
            db.commit()
            db.refresh(existing_record)
        apply_elevenlabs_transcript(
            db,
            existing_record,
            payload_for_apply,
            source="webhook",
        )
        record = existing_record
    else:
        record = CallRecord(
            lead_id=lead.id,
            provider="elevenlabs_sip",
            status=data.get("status", "completed"),
            call_id=conversation_id,
        )
        db.add(record)
        db.commit()
        db.refresh(record)
        apply_elevenlabs_transcript(
            db,
            record,
            payload_for_apply,
            source="webhook",
        )
    logger.info(
        "ElevenLabs transcription mapped: call_record_id=%s status=%s reason=%s",
        record.id,
        record.status,
        reason,
    )
    return {"ok": True, "call_record_id": record.id}
