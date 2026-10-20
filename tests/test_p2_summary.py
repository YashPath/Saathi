"""P2-5 checks: daily summary and the evening job."""

from contextlib import contextmanager
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import scheduler as sched
from app.agent.roles import billing, summary
from app.agent.roles import inventory as inv
from app.agent.roles import shopkeeper as shop
from app.config import get_settings
from app.db.models import Notification, User
from tests.conftest import needs_db

IST = ZoneInfo("Asia/Kolkata")


def test_today_is_the_indian_day():
    # 01:00 IST on 20 Oct is still 19 Oct in UTC; the summary must use the Indian date
    start, end = summary.today_ist(datetime(2026, 10, 20, 1, 0, tzinfo=IST))
    assert start == datetime(2026, 10, 20, 0, 0, tzinfo=IST) and (end - start).days == 1


def test_scheduler_runs_at_summary_hour_ist(monkeypatch):
    monkeypatch.setattr(get_settings(), "summary_hour", 21)
    s = sched.start_scheduler()
    try:
        job = s.get_job("daily_summary")
        fields = {f.name: str(f) for f in job.trigger.fields}
        assert fields["hour"] == "21" and fields["minute"] == "0" and str(job.trigger.timezone) == "Asia/Kolkata"
        reminders = {f.name: str(f) for f in s.get_job("assignment_reminders").trigger.fields}
        assert reminders["hour"] == f"{get_settings().assignment_reminder_hour}-22"  # hourly retries through the evening
    finally:
        s.shutdown(wait=False)


@needs_db
class TestDb:
    @pytest.fixture(autouse=True)
    def files_in_tmp(self, tmp_path, monkeypatch):
        monkeypatch.setattr(get_settings(), "bills_dir", str(tmp_path))

    @pytest.fixture
    def owner(self, session):
        user = User(phone="911", name="Yash Store", role="shopkeeper")
        session.add(user)
        session.flush()
        inv.add_product(session, user.id, "Maggi", price="14", opening_stock="20", reorder_level="25")
        return user.id

    def test_summary_numbers(self, session, owner):
        billing.create_bill(session, owner, [{"product_name": "Maggi", "qty": "5"}])  # cash 70
        billing.create_bill(session, owner, [{"product_name": "Maggi", "qty": "2"}], customer_name="Sharma", on_credit=True)  # credit 28
        shop.add_credit(session, owner, "Gupta", 200)
        shop.record_payment(session, owner, "Gupta", 50)
        shop.record_payment(session, owner, "Sharma", 8)
        shop.undo_last_entry(session, owner)  # the 8 payment never happened
        r = summary.daily_summary(session, owner)
        assert r["bills"] == 2 and r["sales"] == "98.00"
        assert "cash ₹70.00, credit ₹28.00" in r["message"]
        assert r["received"] == "50.00"  # undone payment excluded, and undo entries are not payments
        assert r["credit_given"] == "228.00"
        assert r["total_dues"] == "178.00"  # Sharma 28 + Gupta 150
        assert "Low stock: Maggi (13 pcs)" in r["message"]

    def test_empty_day(self, session, owner):
        r = summary.daily_summary(session, owner)
        assert r["bills"] == 0 and "Total dues: ₹0.00 from 0 customers" in r["message"]

    def test_job_sends_to_every_shop_and_survives_one_failure(self, engine, monkeypatch):
        with Session(engine) as s:
            s.add_all([User(phone="911", name="A", role="shopkeeper"), User(phone="922", name="B", role="shopkeeper"),
                       User(phone="933", name="Prof", role="professor")])
            s.commit()

        @contextmanager
        def test_session():
            with Session(engine) as s:
                yield s
                s.commit()

        real = sched.daily_summary

        def flaky(session, owner_id):
            if session.get(User, owner_id).phone == "911":
                raise RuntimeError("boom")
            return real(session, owner_id)

        monkeypatch.setattr(sched, "get_session", test_session)
        monkeypatch.setattr(sched, "daily_summary", flaky)
        assert sched.send_daily_summaries() == 1
        with Session(engine) as s:
            got = s.execute(select(User.phone, Notification.text).join(Notification, Notification.user_id == User.id)).all()
        assert [p for p, _ in got] == ["922"] and got[0][1].startswith("Today's summary")  # professor gets nothing
