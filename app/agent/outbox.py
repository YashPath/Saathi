"""Messages produced during a turn. Tools queue them; the channel sends them only after the DB commit."""

from sqlalchemy.orm import Session


def queue_text(session: Session, to: str, text: str) -> None:
    session.info.setdefault("outbox", []).append({"to": to, "text": text})


def queue_file(session: Session, to: str, path: str, filename: str, kind: str = "document") -> None:
    """kind is "document" (PDF) or "image" (PNG)."""
    session.info.setdefault("outbox", []).append({"to": to, "file": path, "filename": filename, "kind": kind})


def take(session: Session) -> list[dict]:
    """Remove and return everything queued in this session."""
    return session.info.pop("outbox", [])
