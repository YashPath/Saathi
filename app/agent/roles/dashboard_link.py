"""A tool every role has: send the user a private link to their web dashboard.

The link carries a login token, so it goes straight to the user's WhatsApp through the outbox. The model, the
conversation history and the action log only ever see "link sent" — never the token.
"""

from sqlalchemy.orm import Session

from app.agent.action_log import log_action
from app.agent.outbox import queue_text
from app.config import get_settings
from app.dashboard.auth import MIN_SECRET, make_token
from app.db.models import User


def dashboard_link(session: Session, owner_id: int) -> dict:
    """Send the user a private link to their web dashboard (charts, records, Saathi's activity). Valid for a day."""
    s = get_settings()
    if len(s.dashboard_secret) < MIN_SECRET:
        return {"ok": False, "message": f"The dashboard is not set up yet (DASHBOARD_SECRET needs {MIN_SECRET}+ characters)."}
    user = session.get(User, owner_id)
    url = f"{s.public_base_url.rstrip('/')}/dashboard#t={make_token(owner_id, user.token_version)}"
    queue_text(session, user.phone, f"Your Saathi dashboard (valid {s.dashboard_link_hours} hours, do not share):\n{url}")
    result = {"ok": True, "message": "Dashboard link sent in a separate message."}
    log_action(session, owner_id, "dashboard_link", {}, result)
    return result


def dashboard_logout(session: Session, owner_id: int) -> dict:
    """Stop every dashboard link sent so far (use when a link was shared by mistake). A new link can be asked for."""
    user = session.get(User, owner_id, with_for_update=True)
    user.token_version += 1
    session.flush()
    result = {"ok": True, "message": "Done: all earlier dashboard links stopped working. Ask for a new one any time."}
    log_action(session, owner_id, "dashboard_logout", {}, result)
    return result


SYSTEM_PROMPT = """- "dashboard / report dikhao / website" -> dashboard_link (the link arrives as its own message).
  "dashboard logout / link band karo" -> dashboard_logout.
"""

TOOLS = [dashboard_link, dashboard_logout]
