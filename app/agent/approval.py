"""Approval gate: risky tool calls wait for the user's YES before they run."""

import re
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agent.action_log import log_action
from app.agent.roles.base import Role, ToolFn
from app.config import get_settings
from app.db.models import ActionLog, PendingAction

YES = {"yes", "y", "haan", "haa", "han", "ha", "ok", "okay", "confirm", "ji haan", "haan ji",
       "kar do", "karo", "done", "theek hai", "thik hai", "sahi hai", "bilkul"}
NO = {"no", "n", "nahi", "nahin", "na", "cancel", "mat karo", "rehne do"}
MAX_ANSWER_WORDS = 3  # "haan kar do" is an answer; "haan, Gupta ka balance batao" is a new request
REQUEST_TOOL = "approval_request"  # its own log name, so undo never mistakes it for a ledger entry


def now() -> datetime:
    return datetime.now(timezone.utc)


def _request_log(session: Session, user_id: int) -> ActionLog | None:
    return session.scalars(
        select(ActionLog)
        .where(ActionLog.user_id == user_id, ActionLog.tool_name == REQUEST_TOOL, ActionLog.status == "pending_approval")
        .order_by(ActionLog.id.desc())
        .limit(1)
    ).first()


def _close(session: Session, user_id: int, status: str) -> None:
    """Remove the user's pending action and mark its request log done/rejected."""
    for p in session.scalars(select(PendingAction).where(PendingAction.user_id == user_id)):
        session.delete(p)
    if log := _request_log(session, user_id):
        log.status = status
    session.flush()


def run_tool(session: Session, user_id: int, role: Role, fn: ToolFn, args: dict) -> dict:
    """Run a tool, or park it as a PendingAction if the role's rule says it needs approval."""
    rule = role.approval_rules.get(fn.__name__)
    if not (rule and rule(session, user_id, args)):
        return fn(session, user_id, **args)
    # Any older pending action was cancelled before the agent ran, so this one is from the same message.
    if session.scalars(select(PendingAction).where(PendingAction.user_id == user_id)).first():
        return {"ok": False, "message": "Only one approval can wait at a time. This was NOT done: ask the owner "
                "to confirm the first one, then repeat this request."}
    summary = f"{fn.__name__.replace('_', ' ')}: " + ", ".join(f"{k}={v}" for k, v in args.items() if v is not None)
    ttl = get_settings().approval_ttl_minutes
    session.add(PendingAction(user_id=user_id, tool_name=fn.__name__, args_json=args, summary_text=summary,
                              expires_at=now() + timedelta(minutes=ttl)))
    log_action(session, user_id, REQUEST_TOOL, {"tool": fn.__name__, "args": args}, None, status="pending_approval")
    return {"ok": True, "pending": True,
            "message": f"Needs approval: {summary}. Tap Haan to confirm or Nahi to cancel (valid {ttl} minutes)."}


def classify(text: str) -> str | None:
    """'yes', 'no' or None. A short reply counts if it is, or starts with, a yes/no word or phrase."""
    raw = text.replace("👍", " yes ").replace("👎", " no ")
    if any(ch.isdigit() for ch in raw):  # "ok Gupta 500" is a new request, never a YES to the old payment
        return None
    words = re.sub(r"[^\w\s]", "", raw.lower()).split()
    if not words or len(words) > MAX_ANSWER_WORDS:
        return None
    for label, vocab in (("no", NO), ("yes", YES)):
        if " ".join(words) in vocab or words[0] in vocab or " ".join(words[:2]) in vocab:
            return label
    return None


def cancel_pending(session: Session, user_id: int) -> str | None:
    """Cancel the user's pending action (they moved on). Returns its summary so the reply can say so."""
    pending = session.scalars(select(PendingAction).where(PendingAction.user_id == user_id)).first()
    if pending is None:
        return None
    _close(session, user_id, "rejected")
    return pending.summary_text


def resolve_pending(session: Session, user_id: int, role: Role, text: str) -> str | None:
    """If text is a YES/NO answer to a pending action, handle it and return the reply; else None."""
    answer = classify(text)
    if answer is None:
        return None
    # FOR UPDATE: a second "yes" arriving at the same time waits here, then finds nothing, so it never runs twice
    pending = session.scalars(select(PendingAction).where(PendingAction.user_id == user_id).with_for_update()).first()
    if pending is None:
        return None
    if pending.expires_at < now():
        _close(session, user_id, "rejected")
        return "That request has expired, so nothing was saved. Please ask again."
    if answer == "no":
        _close(session, user_id, "rejected")
        return "Cancelled. Nothing was saved."
    fn, args = role.tool(pending.tool_name), dict(pending.args_json)
    result = fn(session, user_id, **args)  # re-validates: things may have changed since the request
    _close(session, user_id, "done" if result["ok"] else "rejected")
    return result["message"]
