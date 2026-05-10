import hashlib
import os
import secrets
from datetime import datetime, timedelta

from fastapi import Header, HTTPException
from sqlalchemy.orm import Session

from .models import User


ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin123")
TOKEN_TTL_HOURS = 12


def hash_password(password: str) -> str:
    return hashlib.sha256(password.encode("utf-8")).hexdigest()


def verify_password(raw_password: str, password_hash: str) -> bool:
    return hash_password(raw_password) == password_hash


def ensure_default_admin(db: Session) -> None:
    admin = db.query(User).filter(User.username == ADMIN_USERNAME).first()
    if admin:
        if admin.role != "admin":
            admin.role = "admin"
            db.commit()
        return
    admin = User(
        username=ADMIN_USERNAME,
        password_hash=hash_password(ADMIN_PASSWORD),
        role="admin",
    )
    db.add(admin)
    db.commit()


SESSIONS: dict[str, dict] = {}


def create_session(user_id: int, role: str) -> str:
    token = secrets.token_urlsafe(32)
    SESSIONS[token] = {
        "user_id": user_id,
        "role": role,
        "expires_at": datetime.utcnow() + timedelta(hours=TOKEN_TTL_HOURS),
    }
    return token


def _get_bearer_token(authorization: str | None) -> str | None:
    if not authorization:
        return None
    parts = authorization.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    return parts[1].strip()


def revoke_session(authorization: str | None) -> None:
    token = _get_bearer_token(authorization)
    if token:
        SESSIONS.pop(token, None)


def require_admin(authorization: str | None = Header(default=None)) -> dict:
    token = _get_bearer_token(authorization)
    if not token:
        raise HTTPException(status_code=401, detail="Требуется вход администратора")
    session_data = SESSIONS.get(token)
    if not session_data:
        raise HTTPException(status_code=401, detail="Сессия недействительна")
    if session_data["expires_at"] < datetime.utcnow():
        SESSIONS.pop(token, None)
        raise HTTPException(status_code=401, detail="Сессия истекла, войдите снова")
    if session_data["role"] != "admin":
        raise HTTPException(status_code=403, detail="Недостаточно прав")
    return session_data
