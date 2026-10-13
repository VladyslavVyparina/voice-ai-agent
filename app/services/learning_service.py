from datetime import datetime, timedelta
from sqlalchemy.orm import Session
from app.models import LearningProfile, ChatMessage, CallRecord, LearningReport
from app.prompts import (
    CHAT_SYSTEM_PROMPT_TEMPLATE,
    LEARNING_UPDATE_SYSTEM_PROMPT,
    LEARNING_UPDATE_USER_PROMPT_TEMPLATE,
)
from app.services.openai_service import generate_answer


def get_or_create_profile(db: Session, niche: str = "default") -> LearningProfile:
    profile = db.query(LearningProfile).filter(LearningProfile.niche == niche).first()
    if profile:
        return profile
    profile = LearningProfile(niche=niche, learned_context="")
    db.add(profile)
    db.commit()
    db.refresh(profile)
    return profile


def build_system_prompt(db: Session, lead_context: str = "") -> str:
    profile = get_or_create_profile(db)
    return CHAT_SYSTEM_PROMPT_TEMPLATE.format(
        lead_context=lead_context or "Немає даних по лід-картці.",
        learned_context=profile.learned_context or "Поки немає.",
    )


def update_learning_profile(db: Session, new_data: str) -> None:
    profile = get_or_create_profile(db)
    prompt = LEARNING_UPDATE_USER_PROMPT_TEMPLATE.format(
        current=profile.learned_context,
        new_data=new_data,
    )
    updated = generate_answer(
        LEARNING_UPDATE_SYSTEM_PROMPT,
        prompt,
    )
    profile.learned_context = updated
    db.add(profile)
    db.commit()


def run_daily_learning_update(db: Session) -> None:
    """
    Daily self-learning cycle:
    aggregates last 24h chat + call transcripts and updates knowledge profile.
    """
    since = datetime.utcnow() - timedelta(days=1)
    chats = (
        db.query(ChatMessage)
        .filter(ChatMessage.created_at >= since)
        .order_by(ChatMessage.created_at.asc())
        .all()
    )
    calls = (
        db.query(CallRecord)
        .filter(CallRecord.created_at >= since)
        .order_by(CallRecord.created_at.asc())
        .all()
    )
    if not chats and not calls:
        return

    profile = get_or_create_profile(db)
    previous = profile.learned_context or ""
    chat_blob = "\n".join([f"[{m.role}/{m.source}] {m.content}" for m in chats[:500]])
    call_blob = "\n".join([f"[{c.provider}/{c.status}] {c.transcript or ''}" for c in calls[:200]])
    payload = (
        "Щоденне автооновлення знань.\n\n"
        f"Нові чати за 24 години:\n{chat_blob or '-'}\n\n"
        f"Нові дзвінки за 24 години:\n{call_blob or '-'}"
    )
    update_learning_profile(db, payload)

    profile = get_or_create_profile(db)
    updated = profile.learned_context or ""
    summary_prompt = (
        "Сформуй короткий звіт змін бази знань за добу.\n"
        "Формат:\n"
        "- Що додано\n"
        "- Що уточнено\n"
        "- Що змінено в тактиці спілкування\n"
        "До 6 пунктів, лаконічно."
    )
    summary = generate_answer(
        LEARNING_UPDATE_SYSTEM_PROMPT,
        f"{summary_prompt}\n\nБуло:\n{previous}\n\nСтало:\n{updated}",
    )
    report = LearningReport(
        niche=profile.niche,
        period_start=since,
        period_end=datetime.utcnow(),
        chats_count=len(chats),
        calls_count=len(calls),
        change_summary=summary,
        previous_context=previous,
        updated_context=updated,
    )
    db.add(report)
    db.commit()
