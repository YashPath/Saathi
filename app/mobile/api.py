"""API for the Android app. Every route except sign-up and login needs the Bearer token from login."""

import logging
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.agent.graph import FAILED, handle_message
from app.agent.outbox import take
from app.config import get_settings
from app.dashboard.api import current_user, db
from app.db.models import Message, Notification, PendingAction, User
from app.mobile import accounts
from app.mobile.delivery import deliver
from app.mobile.voice import MAX_BYTES, VoiceError, transcribe

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/app")
AUDIO_TYPES = {"audio/aac", "audio/mp4", "audio/mpeg", "audio/ogg", "audio/wav", "audio/webm", "audio/x-m4a"}


class SignUp(BaseModel):
    phone: str
    pin: str
    role: str
    name: str = ""
    setup_code: str | None = None


class Login(BaseModel):
    phone: str
    pin: str


class Chat(BaseModel):
    text: str


def session_reply(user: User) -> dict:
    return {"token": accounts.token_for(user), "user": {"name": user.name, "role": user.role, "phone": user.phone}}


@router.post("/register")
def register(body: SignUp, session: Session = Depends(db)) -> dict:
    try:
        user = accounts.register(session, body.phone, body.name, body.role, body.pin, body.setup_code)
    except accounts.AuthError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return session_reply(user)


@router.post("/login")
def login(body: Login, session: Session = Depends(db)) -> dict:
    try:
        user = accounts.login(session, body.phone, body.pin)
    except accounts.AuthError as exc:
        raise HTTPException(status_code=401, detail=str(exc))
    return session_reply(user)


def run_turn(session: Session, user: User, text: str) -> dict:
    """One chat turn: the agent's reply plus whatever its tools produced, all in this one transaction."""
    reply = handle_message(session, user.phone, text)
    extras = deliver(session, user, take(session)) if reply != FAILED else {"messages": [], "files": [], "shares": []}
    waiting = session.scalar(select(PendingAction.id).where(PendingAction.user_id == user.id)) is not None
    return {"reply": reply, **extras, "quick_replies": ["Haan", "Nahi"] if waiting else []}


@router.post("/chat")
def chat(body: Chat, user: User = Depends(current_user), session: Session = Depends(db)) -> dict:
    text = body.text.strip()
    if not text or len(text) > 2000:
        raise HTTPException(status_code=400, detail="Message must be 1 to 2000 characters.")
    return run_turn(session, user, text)


@router.post("/voice")
async def voice(audio: UploadFile = File(...), user: User = Depends(current_user),
                session: Session = Depends(db)) -> dict:
    mime = (audio.content_type or "").split(";")[0].strip()
    if mime not in AUDIO_TYPES:
        raise HTTPException(status_code=415, detail="Unsupported audio type.")
    data = await audio.read(MAX_BYTES + 1)
    if not data or len(data) > MAX_BYTES:
        raise HTTPException(status_code=413, detail="Voice note must be under 10 MB.")
    try:
        text = transcribe(data, mime)
    except VoiceError as exc:
        log.warning("voice from user %s failed: %s", user.id, exc)
        raise HTTPException(status_code=422, detail="Sorry, I couldn't understand that. Please type it.")
    return {"heard": text, **run_turn(session, user, text)}


@router.get("/inbox")
def inbox(user: User = Depends(current_user), session: Session = Depends(db)) -> dict:
    """New messages from other users and scheduled jobs; each is returned once."""
    rows = session.scalars(select(Notification).where(Notification.user_id == user.id, Notification.seen.is_(False))
                           .order_by(Notification.id).limit(50).with_for_update()).all()
    for row in rows:
        row.seen = True
    return {"items": [{"id": r.id, "text": r.text, "filename": r.filename if r.file_path else None,
                       "at": r.created_at.isoformat()} for r in rows]}


@router.get("/history")
def history(user: User = Depends(current_user), session: Session = Depends(db)) -> dict:
    rows = session.scalars(select(Message).where(Message.user_id == user.id).order_by(Message.id.desc()).limit(50)).all()
    return {"messages": [{"role": m.role, "text": m.content, "at": m.created_at.isoformat()} for m in reversed(rows)]}


@router.get("/file/{file_id}")
def file(file_id: int, user: User = Depends(current_user), session: Session = Depends(db)) -> FileResponse:
    row = session.get(Notification, file_id)
    if row is None or row.user_id != user.id or not row.file_path:
        raise HTTPException(status_code=404, detail="File not found.")
    path = Path(row.file_path).resolve()
    if not path.is_relative_to(Path(get_settings().bills_dir).resolve()) or not path.is_file():
        raise HTTPException(status_code=404, detail="File not found.")
    return FileResponse(path, filename=row.filename, headers={"Cache-Control": "no-store"})


@router.post("/logout")
def logout(user: User = Depends(current_user), session: Session = Depends(db)) -> dict:
    """Log out on every phone: all tokens, including dashboard links, stop working."""
    session.execute(update(User).where(User.id == user.id).values(token_version=User.token_version + 1))
    return {"ok": True}
