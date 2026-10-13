import secrets
from datetime import datetime, timedelta
import bcrypt
from sqlalchemy.orm import Session
from app.models import AdminUser, AdminSession
from app.config import settings


def hash_password(password: str) -> str:
    salt = bcrypt.gensalt(rounds=12)
    return bcrypt.hashpw(password.encode("utf-8"), salt).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except Exception:
        return False


def ensure_default_owner(db: Session) -> None:
    email = (settings.admin_email or "").strip().lower()
    password = (settings.admin_password or settings.admin_token or "").strip()
    if not email or not password:
        return
    existing = db.query(AdminUser).filter(AdminUser.email == email).first()
    if existing:
        return
    user = AdminUser(
        email=email,
        password_hash=hash_password(password),
        role="owner",
        is_active=1,
    )
    db.add(user)
    db.commit()


def create_session(db: Session, user: AdminUser, days: int = 30) -> str:
    token = secrets.token_urlsafe(48)
    sess = AdminSession(
        user_id=user.id,
        token=token,
        expires_at=datetime.utcnow() + timedelta(days=days),
    )
    db.add(sess)
    db.commit()
    return token


def get_user_by_session_token(db: Session, token: str) -> AdminUser | None:
    if not token:
        return None
    session = (
        db.query(AdminSession)
        .filter(AdminSession.token == token, AdminSession.expires_at > datetime.utcnow())
        .first()
    )
    if not session:
        return None
    user = db.query(AdminUser).filter(AdminUser.id == session.user_id, AdminUser.is_active == 1).first()
    return user


def delete_session(db: Session, token: str) -> None:
    if not token:
        return
    db.query(AdminSession).filter(AdminSession.token == token).delete()
    db.commit()
