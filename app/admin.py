import csv
import io
import json
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from fastapi import APIRouter, Depends, Request, HTTPException, UploadFile, File, Form
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from app.database import get_db
from app.models import (
    Lead,
    ChatMessage,
    CallRecord,
    LearningProfile,
    LearningReport,
    AdminUser,
    AdminProfile,
)
from app.config import settings
from app.services.telephony_service import initiate_ai_call
from app.services.call_transcript_service import (
    friendly_call_status,
    is_placeholder_transcript,
    parse_call_summary,
    sync_elevenlabs_call_record,
)
from app.services.learning_service import run_daily_learning_update
from app.services.auth_service import (
    ensure_default_owner,
    verify_password,
    create_session,
    get_user_by_session_token,
    delete_session,
    hash_password,
)

templates = Jinja2Templates(directory="templates")
router = APIRouter()
ADMIN_COOKIE_NAME = "admin_auth_token"
STALE_CALL_TIMEOUT_MINUTES = 20
UTC_TZ = ZoneInfo("UTC")
KYIV_TZ = ZoneInfo("Europe/Kyiv")


def dt_kyiv(value: datetime | None) -> str:
    if not value:
        return "-"
    dt_value = value.replace(tzinfo=UTC_TZ) if value.tzinfo is None else value
    return dt_value.astimezone(KYIV_TZ).strftime("%Y-%m-%d %H:%M:%S")


templates.env.filters["dt_kyiv"] = dt_kyiv
templates.env.filters["call_summary_text"] = parse_call_summary
templates.env.filters["is_real_transcript"] = lambda text: not is_placeholder_transcript(text)
templates.env.filters["friendly_call_status"] = friendly_call_status


def _legacy_header_owner(db: Session, request: Request) -> AdminUser | None:
    token = request.headers.get("x-admin-token", "")
    if not token or token != settings.admin_token:
        return None
    return db.query(AdminUser).filter(AdminUser.role == "owner", AdminUser.is_active == 1).first()


def get_current_admin(request: Request, db: Session = Depends(get_db)) -> AdminUser:
    ensure_default_owner(db)
    token = request.cookies.get(ADMIN_COOKIE_NAME, "")
    user = get_user_by_session_token(db, token) if token else None
    if not user:
        user = _legacy_header_owner(db, request)
    if not user:
        raise HTTPException(status_code=303, detail="Redirect to login", headers={"Location": "/admin/login"})
    return user


def require_owner(admin_user: AdminUser = Depends(get_current_admin)) -> AdminUser:
    if admin_user.role != "owner":
        raise HTTPException(status_code=403, detail="Owner role required")
    return admin_user


def _normalize_phone(value: str | None) -> str:
    digits = "".join(ch for ch in (value or "") if ch.isdigit())
    if not digits:
        return ""
    if digits.startswith("380") and len(digits) == 12:
        return digits
    if digits.startswith("0") and len(digits) == 10:
        return "38" + digits
    return ""


def _find_lead_by_phone(db: Session, phone: str) -> Lead | None:
    normalized = _normalize_phone(phone)
    if not normalized:
        return None
    candidates = db.query(Lead).filter(Lead.phone.isnot(None)).all()
    for lead in candidates:
        if _normalize_phone(lead.phone) == normalized:
            return lead
    return None


def _is_call_in_progress(record: CallRecord | None) -> bool:
    if not record:
        return False
    in_progress_statuses = {"initiated", "ringing", "in_progress", "started"}
    if (record.status or "").lower() not in in_progress_statuses:
        return False
    # Prevent stale "in progress" badge for old records.
    return record.created_at >= datetime.utcnow() - timedelta(minutes=STALE_CALL_TIMEOUT_MINUTES)


def _close_stale_call_if_needed(db: Session, record: CallRecord | None) -> CallRecord | None:
    if not record:
        return None

    # Always try to pull a real transcript for ElevenLabs placeholder records.
    if record.provider == "elevenlabs_sip" and is_placeholder_transcript(record.transcript):
        sync_elevenlabs_call_record(db, record)
        db.refresh(record)
        if not is_placeholder_transcript(record.transcript):
            return record

    in_progress_statuses = {"initiated", "ringing", "in_progress", "started", "processing"}
    status = (record.status or "").lower()
    if status not in in_progress_statuses:
        return record
    if _is_call_in_progress(record):
        return record

    if record.provider == "elevenlabs_sip":
        record.status = "no_webhook_timeout"
        record.summary = json.dumps(
            {
                "summary": "Очікуємо транскрипцію",
                "reason": "",
                "status": "awaiting_transcript",
            },
            ensure_ascii=False,
        )
        db.add(record)
        db.commit()
        db.refresh(record)
        return record

    reason = "No final webhook status received in time"
    summary_payload = {"summary": "Auto-closed stale call", "reason": reason, "status": "no_webhook_timeout"}
    record.status = "no_webhook_timeout"
    record.summary = json.dumps(summary_payload, ensure_ascii=False)
    db.add(record)
    db.commit()
    db.refresh(record)
    return record


def _extract_call_reason(record: CallRecord) -> str:
    status = (record.status or "").lower()
    if status in {"completed", "done", "success", "successful", "initiated", "ringing", "in_progress", "started"}:
        return ""
    if status == "no_webhook_timeout":
        return ""

    summary_text = (record.summary or "").strip()
    if summary_text:
        try:
            parsed = json.loads(summary_text)
            for key in ("reason", "error", "detail", "message", "termination_reason"):
                value = parsed.get(key)
                if value:
                    text = str(value).strip()
                    if text.lower() in {
                        "outbound call initiated",
                        "event update",
                        "auto-closed stale call",
                        "no final webhook status received in time",
                    }:
                        continue
                    return text
        except Exception:
            match = re.search(
                r"(reason|error|detail|message|termination_reason)['\"]?\s*[:=]\s*['\"]([^'\"]+)['\"]",
                summary_text,
                flags=re.IGNORECASE,
            )
            if match:
                return match.group(2).strip()

    transcript_text = (record.transcript or "").lower()
    if "error" in transcript_text and not is_placeholder_transcript(record.transcript):
        return "Call ended with error (see transcript)"
    return ""


def _get_or_create_profile(db: Session, user_id: int) -> AdminProfile:
    profile = db.query(AdminProfile).filter(AdminProfile.user_id == user_id).first()
    if profile:
        return profile
    profile = AdminProfile(user_id=user_id, display_name="", avatar_url="")
    db.add(profile)
    db.commit()
    db.refresh(profile)
    return profile


@router.get("/login", response_class=HTMLResponse)
def admin_login_page(request: Request, db: Session = Depends(get_db)):
    ensure_default_owner(db)
    return templates.TemplateResponse(
        "admin_login.html",
        {"request": request, "error": None, "default_email": settings.admin_email},
    )


@router.post("/login", response_class=HTMLResponse)
def admin_login_submit(request: Request, email: str = Form(""), password: str = Form(""), db: Session = Depends(get_db)):
    ensure_default_owner(db)
    user = db.query(AdminUser).filter(AdminUser.email == email.strip().lower(), AdminUser.is_active == 1).first()
    if not user or not verify_password(password.strip(), user.password_hash):
        return templates.TemplateResponse(
            "admin_login.html",
            {"request": request, "error": "Невірний логін або пароль.", "default_email": settings.admin_email},
            status_code=401,
        )
    session_token = create_session(db, user=user, days=30)
    response = RedirectResponse(url="/admin", status_code=303)
    response.set_cookie(
        key=ADMIN_COOKIE_NAME,
        value=session_token,
        httponly=True,
        secure=request.url.scheme == "https",
        samesite="lax",
        max_age=60 * 60 * 24 * 30,
    )
    return response


@router.get("/logout")
def admin_logout(request: Request, db: Session = Depends(get_db)):
    token = request.cookies.get(ADMIN_COOKIE_NAME, "")
    if token:
        delete_session(db, token)
    response = RedirectResponse(url="/admin/login", status_code=303)
    response.delete_cookie(ADMIN_COOKIE_NAME)
    return response


@router.get("/", response_class=HTMLResponse)
def admin_leads(
    request: Request,
    db: Session = Depends(get_db),
    admin_user: AdminUser = Depends(get_current_admin),
):
    leads = db.query(Lead).order_by(Lead.created_at.desc()).all()
    reports_count = db.query(LearningReport).count()
    profile = _get_or_create_profile(db, admin_user.id)
    return templates.TemplateResponse(
        "leads.html",
        {
            "request": request,
            "leads": leads,
            "reports_count": reports_count,
            "admin_user": admin_user,
            "admin_profile": profile,
        },
    )


@router.get("/ai-knowledge", response_class=HTMLResponse)
def ai_knowledge_page(
    request: Request,
    db: Session = Depends(get_db),
    admin_user: AdminUser = Depends(get_current_admin),
):
    profile = db.query(LearningProfile).filter(LearningProfile.niche == "default").first()
    reports = db.query(LearningReport).order_by(LearningReport.created_at.desc()).limit(30).all()
    chart_labels = [r.created_at.strftime("%d.%m") for r in reversed(reports)]
    chart_chats = [r.chats_count for r in reversed(reports)]
    chart_calls = [r.calls_count for r in reversed(reports)]
    profile_admin = _get_or_create_profile(db, admin_user.id)
    return templates.TemplateResponse(
        "ai_knowledge.html",
        {
            "request": request,
            "profile": profile,
            "reports": reports,
            "chart_labels": chart_labels,
            "chart_chats": chart_chats,
            "chart_calls": chart_calls,
            "admin_user": admin_user,
            "admin_profile": profile_admin,
        },
    )


@router.get("/telephony-diagnostics", response_class=HTMLResponse)
def telephony_diagnostics_page(
    request: Request,
    db: Session = Depends(get_db),
    admin_user: AdminUser = Depends(get_current_admin),
):
    recent_calls = db.query(CallRecord).order_by(CallRecord.created_at.desc()).limit(30).all()
    normalized_recent_calls: list[CallRecord] = []
    for rec in recent_calls:
        normalized_recent_calls.append(_close_stale_call_if_needed(db, rec) or rec)
    provider = settings.telephony_provider
    checks = [
        ("TELEPHONY_PROVIDER", provider),
        ("PUBLIC_BASE_URL", settings.public_base_url),
        ("ELEVENLABS_API_KEY", "set" if bool(settings.elevenlabs_api_key) else "missing"),
        ("ELEVENLABS_AGENT_ID", "set" if bool(settings.elevenlabs_agent_id) else "missing"),
        (
            "ELEVENLABS_AGENT_PHONE_NUMBER_ID",
            "set" if bool(settings.elevenlabs_agent_phone_number_id) else "missing",
        ),
        ("ZADARMA_KEY", "set" if bool(settings.zadarma_key) else "missing"),
        ("ZADARMA_FROM", settings.zadarma_from or "-"),
        ("ZADARMA_SCENARIO_ID", settings.zadarma_scenario_id or "-"),
    ]
    ok_count = sum(1 for _, v in checks if v not in {"missing", "-", ""})
    recent_calls_view = [
        {
            "id": c.id,
            "created_at": c.created_at,
            "provider": c.provider,
            "status": c.status,
            "call_id": c.call_id,
            "summary": c.summary,
            "reason": _extract_call_reason(c),
        }
        for c in normalized_recent_calls
    ]
    profile_admin = _get_or_create_profile(db, admin_user.id)
    return templates.TemplateResponse(
        "telephony_diagnostics.html",
        {
            "request": request,
            "admin_user": admin_user,
            "admin_profile": profile_admin,
            "recent_calls": recent_calls_view,
            "checks": checks,
            "ok_count": ok_count,
            "checks_total": len(checks),
            "webhook_url": f"{settings.public_base_url.rstrip('/')}/telephony/elevenlabs/webhook",
        },
    )


@router.post("/ai-knowledge/update")
def ai_knowledge_update(
    request: Request,
    db: Session = Depends(get_db),
    admin_user: AdminUser = Depends(get_current_admin),
):
    run_daily_learning_update(db)
    return RedirectResponse(url="/admin/ai-knowledge", status_code=303)


@router.post("/ai-knowledge/save")
def ai_knowledge_save(
    request: Request,
    learned_context: str = Form(""),
    db: Session = Depends(get_db),
    _: AdminUser = Depends(require_owner),
):
    profile = db.query(LearningProfile).filter(LearningProfile.niche == "default").first()
    if not profile:
        profile = LearningProfile(niche="default", learned_context=learned_context)
    else:
        profile.learned_context = learned_context
    db.add(profile)
    db.commit()
    return RedirectResponse(url="/admin/ai-knowledge", status_code=303)


@router.get("/users", response_class=HTMLResponse)
def admin_users_page(
    request: Request,
    db: Session = Depends(get_db),
    admin_user: AdminUser = Depends(require_owner),
):
    users = db.query(AdminUser).order_by(AdminUser.created_at.desc()).all()
    profile_admin = _get_or_create_profile(db, admin_user.id)
    return templates.TemplateResponse(
        "admin_users.html",
        {
            "request": request,
            "users": users,
            "admin_user": admin_user,
            "admin_profile": profile_admin,
            "default_email": settings.admin_email,
        },
    )


@router.post("/users")
def admin_users_create(
    request: Request,
    email: str = Form(""),
    password: str = Form(""),
    role: str = Form("manager"),
    db: Session = Depends(get_db),
    _: AdminUser = Depends(require_owner),
):
    email_clean = email.strip().lower()
    if role not in {"owner", "manager"}:
        raise HTTPException(status_code=400, detail="Invalid role")
    if not email_clean or not password.strip():
        raise HTTPException(status_code=400, detail="Email and password required")
    existing = db.query(AdminUser).filter(AdminUser.email == email_clean).first()
    if existing:
        raise HTTPException(status_code=400, detail="User already exists")
    user = AdminUser(
        email=email_clean,
        password_hash=hash_password(password.strip()),
        role=role,
        is_active=1,
    )
    db.add(user)
    db.commit()
    return RedirectResponse(url="/admin/users", status_code=303)


@router.post("/users/{user_id}/toggle")
def admin_users_toggle(
    user_id: int,
    request: Request,
    db: Session = Depends(get_db),
    _: AdminUser = Depends(require_owner),
):
    user = db.query(AdminUser).filter(AdminUser.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    user.is_active = 0 if user.is_active else 1
    user.updated_at = datetime.utcnow()
    db.add(user)
    db.commit()
    return RedirectResponse(url="/admin/users", status_code=303)


@router.get("/lead/new", response_class=HTMLResponse)
def lead_new_page(
    request: Request,
    db: Session = Depends(get_db),
    admin_user: AdminUser = Depends(get_current_admin),
):
    profile_admin = _get_or_create_profile(db, admin_user.id)
    return templates.TemplateResponse(
        "lead_new.html",
        {"request": request, "admin_user": admin_user, "admin_profile": profile_admin, "error": None},
    )


@router.post("/lead/new")
def lead_new_submit(
    request: Request,
    name: str = Form(""),
    phone: str = Form(""),
    needs: str = Form(""),
    telegram_user_id: str = Form("manual"),
    username: str = Form(""),
    db: Session = Depends(get_db),
    _: AdminUser = Depends(get_current_admin),
):
    if not (name.strip() or phone.strip() or needs.strip()):
        raise HTTPException(status_code=400, detail="At least one field is required")
    phone_normalized = _normalize_phone(phone.strip()) if phone.strip() else ""
    if phone.strip() and not phone_normalized:
        profile_admin = _get_or_create_profile(db, _.id)
        return templates.TemplateResponse(
            "lead_new.html",
            {
                "request": request,
                "admin_user": _,
                "admin_profile": profile_admin,
                "error": "Вкажіть номер у форматі +380XXXXXXXXX, 380XXXXXXXXX або 0XXXXXXXXX.",
            },
            status_code=400,
        )
    if phone_normalized:
        duplicate = _find_lead_by_phone(db, phone_normalized)
        if duplicate:
            profile_admin = _get_or_create_profile(db, _.id)
            return templates.TemplateResponse(
                "lead_new.html",
                {
                    "request": request,
                    "admin_user": _,
                    "admin_profile": profile_admin,
                    "error": f"Лід з номером уже існує (ID #{duplicate.id}).",
                },
                status_code=400,
            )

    lead = Lead(
        telegram_user_id=telegram_user_id.strip() or "manual",
        username=username.strip() or None,
        name=name.strip() or None,
        phone=phone_normalized or None,
        needs=needs.strip() or None,
        status="manual",
    )
    db.add(lead)
    db.commit()
    db.refresh(lead)
    return RedirectResponse(url=f"/admin/lead/{lead.id}", status_code=303)


@router.get("/lead/{lead_id}", response_class=HTMLResponse)
def lead_detail(
    lead_id: int,
    request: Request,
    db: Session = Depends(get_db),
    admin_user: AdminUser = Depends(get_current_admin),
):
    lead = db.query(Lead).filter(Lead.id == lead_id).first()
    if not lead:
        raise HTTPException(status_code=404, detail="Lead not found")
    profile_admin = _get_or_create_profile(db, admin_user.id)
    latest_call = (
        db.query(CallRecord)
        .filter(CallRecord.lead_id == lead.id)
        .order_by(CallRecord.created_at.desc())
        .first()
    )
    latest_call = _close_stale_call_if_needed(db, latest_call)
    call_in_progress = _is_call_in_progress(latest_call)
    call_records = (
        db.query(CallRecord)
        .filter(CallRecord.lead_id == lead.id)
        .order_by(CallRecord.created_at.desc())
        .all()
    )
    return templates.TemplateResponse(
        "lead_detail.html",
        {
            "request": request,
            "lead": lead,
            "admin_user": admin_user,
            "admin_profile": profile_admin,
            "latest_call": latest_call,
            "call_records": call_records,
            "call_in_progress": call_in_progress,
        },
    )


@router.post("/lead/{lead_id}/call")
def trigger_call(
    lead_id: int,
    request: Request,
    db: Session = Depends(get_db),
    _: AdminUser = Depends(get_current_admin),
):
    lead = db.query(Lead).filter(Lead.id == lead_id).first()
    if not lead:
        raise HTTPException(status_code=404, detail="Lead not found")
    try:
        initiate_ai_call(db, lead)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Call initiation failed: {exc}") from exc
    return RedirectResponse(url=f"/admin/lead/{lead_id}", status_code=303)


@router.post("/lead/{lead_id}/sync-transcript")
def sync_lead_transcript(
    lead_id: int,
    request: Request,
    db: Session = Depends(get_db),
    _: AdminUser = Depends(get_current_admin),
):
    lead = db.query(Lead).filter(Lead.id == lead_id).first()
    if not lead:
        raise HTTPException(status_code=404, detail="Lead not found")
    call_records = (
        db.query(CallRecord)
        .filter(CallRecord.lead_id == lead_id, CallRecord.provider == "elevenlabs_sip")
        .order_by(CallRecord.created_at.desc())
        .all()
    )
    for record in call_records:
        sync_elevenlabs_call_record(db, record)
    db.commit()
    return RedirectResponse(url=f"/admin/lead/{lead_id}", status_code=303)


@router.get("/lead/{lead_id}/call-status")
def lead_call_status(
    lead_id: int,
    request: Request,
    db: Session = Depends(get_db),
    _: AdminUser = Depends(get_current_admin),
):
    latest_call = (
        db.query(CallRecord)
        .filter(CallRecord.lead_id == lead_id)
        .order_by(CallRecord.created_at.desc())
        .first()
    )
    if not latest_call:
        return {"ok": True, "status": "none"}
    latest_call = _close_stale_call_if_needed(db, latest_call)
    if latest_call and latest_call.provider == "elevenlabs_sip":
        sync_elevenlabs_call_record(db, latest_call)
        db.refresh(latest_call)
    in_progress = _is_call_in_progress(latest_call)
    has_transcript = bool(latest_call and not is_placeholder_transcript(latest_call.transcript))
    return {
        "ok": True,
        "status": latest_call.status,
        "in_progress": in_progress,
        "has_transcript": has_transcript,
        "provider": latest_call.provider,
        "call_id": latest_call.call_id or "",
        "created_at": latest_call.created_at.strftime("%Y-%m-%d %H:%M:%S"),
    }


@router.post("/lead/{lead_id}/delete")
def delete_lead(
    lead_id: int,
    request: Request,
    db: Session = Depends(get_db),
    _: AdminUser = Depends(require_owner),
):
    lead = db.query(Lead).filter(Lead.id == lead_id).first()
    if not lead:
        raise HTTPException(status_code=404, detail="Lead not found")
    db.delete(lead)
    db.commit()
    return RedirectResponse(url="/admin", status_code=303)


@router.get("/demo-calls", response_class=HTMLResponse)
def demo_calls_page(
    request: Request,
    db: Session = Depends(get_db),
    admin_user: AdminUser = Depends(get_current_admin),
):
    demo_leads = (
        db.query(Lead)
        .filter(Lead.source == "website_demo")
        .order_by(Lead.created_at.desc())
        .all()
    )
    lead_calls = {}
    for lead in demo_leads:
        latest_call = (
            db.query(CallRecord)
            .filter(CallRecord.lead_id == lead.id)
            .order_by(CallRecord.created_at.desc())
            .first()
        )
        latest_call = _close_stale_call_if_needed(db, latest_call)
        lead_calls[lead.id] = latest_call

    profile_admin = _get_or_create_profile(db, admin_user.id)
    return templates.TemplateResponse(
        "demo_calls.html",
        {
            "request": request,
            "leads": demo_leads,
            "lead_calls": lead_calls,
            "admin_user": admin_user,
            "admin_profile": profile_admin,
            "is_call_in_progress": _is_call_in_progress,
            "extract_call_reason": _extract_call_reason,
            "friendly_call_status": friendly_call_status,
            "is_placeholder_transcript": is_placeholder_transcript,
        },
    )


@router.get("/export/csv")
def export_csv(request: Request, db: Session = Depends(get_db), _: AdminUser = Depends(get_current_admin)):
    leads = db.query(Lead).order_by(Lead.created_at.asc()).all()
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["id", "telegram_user_id", "username", "name", "phone", "needs", "status", "created_at"])
    for lead in leads:
        writer.writerow(
            [
                lead.id,
                lead.telegram_user_id,
                lead.username or "",
                lead.name or "",
                lead.phone or "",
                lead.needs or "",
                lead.status or "",
                lead.created_at.strftime("%Y-%m-%d %H:%M:%S"),
            ]
        )
    csv_data = buffer.getvalue()
    headers = {"Content-Disposition": "attachment; filename=leads_export.csv"}
    return Response(content=csv_data, media_type="text/csv", headers=headers)


@router.get("/export/json")
def export_json(request: Request, db: Session = Depends(get_db), _: AdminUser = Depends(get_current_admin)):
    leads = db.query(Lead).order_by(Lead.created_at.asc()).all()
    result = []
    for lead in leads:
        result.append(
            {
                "id": lead.id,
                "telegram_user_id": lead.telegram_user_id,
                "username": lead.username,
                "name": lead.name,
                "phone": lead.phone,
                "needs": lead.needs,
                "status": lead.status,
                "created_at": str(lead.created_at),
                "updated_at": str(lead.updated_at),
                "chat_messages": [
                    {
                        "role": msg.role,
                        "content": msg.content,
                        "source": msg.source,
                        "created_at": str(msg.created_at),
                    }
                    for msg in lead.chat_messages
                ],
                "call_records": [
                    {
                        "provider": call.provider,
                        "status": call.status,
                        "call_id": call.call_id,
                        "transcript": call.transcript,
                        "summary": call.summary,
                        "created_at": str(call.created_at),
                    }
                    for call in lead.call_records
                ],
            }
        )
    headers = {"Content-Disposition": "attachment; filename=leads_export.json"}
    return Response(
        content=json.dumps(result, ensure_ascii=False, indent=2),
        media_type="application/json",
        headers=headers,
    )


@router.post("/import")
async def import_data(
    request: Request,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    _: AdminUser = Depends(get_current_admin),
):
    filename = (file.filename or "").lower()
    raw = await file.read()
    if not raw:
        raise HTTPException(status_code=400, detail="Empty import file")

    imported = 0
    if filename.endswith(".csv"):
        text = raw.decode("utf-8")
        reader = csv.DictReader(io.StringIO(text))
        for row in reader:
            lead = Lead(
                telegram_user_id=(row.get("telegram_user_id") or "imported").strip() or "imported",
                username=(row.get("username") or "").strip() or None,
                name=(row.get("name") or "").strip() or None,
                phone=_normalize_phone((row.get("phone") or "").strip()) or None,
                needs=(row.get("needs") or "").strip() or None,
                status=(row.get("status") or "imported").strip() or "imported",
            )
            if lead.phone and _find_lead_by_phone(db, lead.phone):
                continue
            db.add(lead)
            imported += 1
    elif filename.endswith(".json"):
        payload = json.loads(raw.decode("utf-8"))
        if not isinstance(payload, list):
            raise HTTPException(status_code=400, detail="JSON must contain a list of leads")
        for item in payload:
            lead = Lead(
                telegram_user_id=str(item.get("telegram_user_id") or "imported"),
                username=item.get("username"),
                name=item.get("name"),
                phone=_normalize_phone(str(item.get("phone") or "")) or None,
                needs=item.get("needs"),
                status=item.get("status") or "imported",
            )
            if lead.phone and _find_lead_by_phone(db, lead.phone):
                continue
            db.add(lead)
            db.flush()
            for msg in item.get("chat_messages", []):
                db.add(
                    ChatMessage(
                        lead_id=lead.id,
                        role=msg.get("role") or "user",
                        content=msg.get("content") or "",
                        source=msg.get("source") or "text",
                    )
                )
            for call in item.get("call_records", []):
                db.add(
                    CallRecord(
                        lead_id=lead.id,
                        provider=call.get("provider") or "zadarma",
                        status=call.get("status") or "completed",
                        call_id=call.get("call_id"),
                        transcript=call.get("transcript"),
                        summary=call.get("summary"),
                    )
                )
            imported += 1
    else:
        raise HTTPException(status_code=400, detail="Unsupported format. Use .csv or .json")

    db.commit()
    return RedirectResponse(url=f"/admin/?imported={imported}", status_code=303)


@router.get("/profile", response_class=HTMLResponse)
def admin_profile_page(
    request: Request,
    db: Session = Depends(get_db),
    admin_user: AdminUser = Depends(get_current_admin),
):
    profile = _get_or_create_profile(db, admin_user.id)
    return templates.TemplateResponse(
        "admin_profile.html",
        {"request": request, "admin_user": admin_user, "admin_profile": profile, "saved": False},
    )


@router.post("/profile")
def admin_profile_save(
    request: Request,
    display_name: str = Form(""),
    avatar_url: str = Form(""),
    db: Session = Depends(get_db),
    admin_user: AdminUser = Depends(get_current_admin),
):
    profile = _get_or_create_profile(db, admin_user.id)
    profile.display_name = display_name.strip()
    profile.avatar_url = avatar_url.strip()
    db.add(profile)
    db.commit()
    db.refresh(profile)
    return templates.TemplateResponse(
        "admin_profile.html",
        {"request": request, "admin_user": admin_user, "admin_profile": profile, "saved": True},
    )
