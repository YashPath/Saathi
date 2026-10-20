"""Background jobs: the evening summary for every shopkeeper and assignment reminders for students."""

import logging
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import select

from app.agent.roles.professor import due_tomorrow_reminders
from app.agent.roles.summary import daily_summary
from app.config import get_settings
from app.db.models import User
from app.db.session import get_session
from app.mobile.delivery import deliver, notify

log = logging.getLogger(__name__)


def send_daily_summaries() -> int:
    """Put today's summary in each shopkeeper's app inbox. One shop failing never stops the others."""
    with get_session() as session:
        shops = session.scalars(select(User.id).where(User.role == "shopkeeper")).all()
    sent = 0
    for owner_id in shops:
        try:
            with get_session() as session:
                notify(session, owner_id, daily_summary(session, owner_id)["message"])
            sent += 1
        except Exception:
            log.exception("daily summary failed for user %s", owner_id)
    return sent


def send_assignment_reminders() -> int:
    """Remind joined students of assignments due tomorrow, in their app inbox. Runs hourly in the evening window, so
    a server that was down at the first run, or an assignment added later, still gets its reminder. The reminder and
    the "reminded" mark are written in one transaction, so it is never sent twice or lost."""
    with get_session() as session:
        messages = due_tomorrow_reminders(session)
        deliver(session, None, [{"to": phone, "text": text} for _, phone, text in messages])
    return len(messages)


def start_scheduler() -> BackgroundScheduler:
    # shortcut: runs inside the web process, so with several workers each would send; use one worker or move to cron
    s = get_settings()
    scheduler = BackgroundScheduler(timezone=ZoneInfo("Asia/Kolkata"))
    scheduler.add_job(send_daily_summaries, CronTrigger(hour=s.summary_hour, minute=0, timezone=ZoneInfo("Asia/Kolkata")),
                      id="daily_summary", replace_existing=True)
    window = f"{s.assignment_reminder_hour}-{max(s.assignment_reminder_hour, 22)}"  # e.g. 18-22: hourly retries
    scheduler.add_job(send_assignment_reminders,
                      CronTrigger(hour=window, minute=0, timezone=ZoneInfo("Asia/Kolkata")),
                      id="assignment_reminders", replace_existing=True)
    scheduler.start()
    return scheduler
