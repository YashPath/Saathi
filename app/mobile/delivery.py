"""Turn the outbox into app deliveries, inside the same transaction as the work that produced it.

- to the person chatting: returned with the reply (texts and files)
- to another Saathi user (a student, a professor): a notification in their app inbox
- to someone without the app (a supplier, a customer): a share card; the owner taps it to send on WhatsApp/SMS
"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Notification, User


def deliver(session: Session, actor: User | None, items: list[dict]) -> dict:
    """Returns {"messages": [text], "files": [{id, filename}], "shares": [{to, text, file_id?}]} for the actor."""
    out: dict = {"messages": [], "files": [], "shares": []}
    for item in items:
        to = item["to"]
        user = session.scalar(select(User).where(User.phone == to))
        is_actor = actor is not None and to == actor.phone
        if user is not None and not is_actor:
            session.add(Notification(user_id=user.id, text=item.get("text"), file_path=item.get("file"),
                                     filename=item.get("filename")))
            continue
        if "file" in item:  # the actor keeps every file; one meant for an outsider rides on their share card
            if actor is None:
                continue
            note = Notification(user_id=actor.id, file_path=item["file"], filename=item["filename"], seen=True)
            session.add(note)
            session.flush()
            if is_actor:
                out["files"].append({"id": note.id, "filename": note.filename})
            else:
                card = next((c for c in out["shares"] if c["to"] == to), None)
                if card:
                    card["file_id"] = note.id
                else:
                    out["shares"].append({"to": to, "text": "", "file_id": note.id})
        elif is_actor:
            out["messages"].append(item["text"])
        else:
            card = next((c for c in out["shares"] if c["to"] == to and not c["text"]), None)
            if card:
                card["text"] = item["text"]
            else:
                out["shares"].append({"to": to, "text": item["text"]})
    session.flush()
    return out


def notify(session: Session, user_id: int, text: str) -> None:
    """For scheduled jobs: put a message in one user's inbox."""
    session.add(Notification(user_id=user_id, text=text))
