"""P3-2 checks: marks, assignments, class messages, the reminder job."""

from datetime import timedelta
from decimal import Decimal

import pytest
from openpyxl import load_workbook
from sqlalchemy import func, select

from app.agent import graph, outbox
from app.agent.roles import professor as prof
from app.config import get_settings
from app.db.models import Assignment, Mark, PendingAction, User
from tests.conftest import accept_invites, fake_llm, needs_db

pytestmark = needs_db


@pytest.fixture(autouse=True)
def files_in_tmp(tmp_path, monkeypatch):
    monkeypatch.setattr(get_settings(), "bills_dir", str(tmp_path))


@pytest.fixture
def owner(session):
    user = User(phone="922", name="Prof Rao", role="professor")
    session.add(user)
    session.flush()
    prof.add_class(session, user.id, "CSE-3")
    prof.add_students(session, user.id, "CSE-3", [{"roll_no": 1, "name": "Ravi", "phone": "9811111111"},
                                                  {"roll_no": 2, "name": "Neha"}, {"roll_no": 3, "name": "Aman", "phone": "9833333333"}])
    accept_invites(session)
    return user.id


def count(session, model):
    return session.scalar(select(func.count()).select_from(model))


def test_marks_and_summary_with_excel(session, owner):
    prof.record_marks(session, owner, "CSE-3", "Mid-term 1", "20",
                      [{"roll_no": 1, "score": 18}, {"roll_no": 2, "score": "12.5"}, {"roll_no": 3, "score": 15}])
    r = prof.marks_summary(session, owner, "cse-3", "mid-term 1")
    assert "Average 15.17, highest 18 (Ravi), lowest 12.5 (Neha)" in r["message"]
    [item] = outbox.take(session)
    assert item["to"] == "922" and item["filename"].endswith(".xlsx")
    rows = list(load_workbook(item["file"]).active.values)
    assert rows[0] == ("Roll", "Name", "Score", "Out of", "Percent") and rows[1] == (1, "Ravi", 18.0, 20.0, 90.0)


def test_resaving_updates_not_duplicates(session, owner):
    prof.record_marks(session, owner, "CSE-3", "Quiz", "10", [{"roll_no": 1, "score": 5}])
    prof.record_marks(session, owner, "CSE-3", "quiz", "10", [{"roll_no": 1, "score": 7}])
    assert count(session, Mark) == 1 and session.scalar(select(Mark.score)) == Decimal("7")


@pytest.mark.parametrize("max_marks,marks", [
    ("20", [{"roll_no": 1, "score": 21}]), ("20", [{"roll_no": 1, "score": -1}]), ("0", [{"roll_no": 1, "score": 0}]),
    ("20", [{"roll_no": 9, "score": 5}]), ("20", [{"roll_no": 1, "score": 5}, {"roll_no": 1, "score": 6}]),
    ("20", [{"roll_no": 1, "score": 5}, {"roll_no": 2, "score": "NaN"}]), ("20", []),
])
def test_bad_marks_save_nothing(session, owner, max_marks, marks):
    assert not prof.record_marks(session, owner, "CSE-3", "Quiz", max_marks, marks)["ok"]
    assert count(session, Mark) == 0


def test_assignment_dates(session, owner):
    tomorrow = (prof.today() + timedelta(days=1)).isoformat()
    assert prof.add_assignment(session, owner, "CSE-3", "Assignment 2", tomorrow)["ok"]
    assert not prof.add_assignment(session, owner, "CSE-3", "Old", (prof.today() - timedelta(days=1)).isoformat())["ok"]
    assert not prof.add_assignment(session, owner, "CSE-3", "Bad", "Friday")["ok"]


def test_due_tomorrow_job_reminds_once(session, owner):
    prof.add_assignment(session, owner, "CSE-3", "Assignment 2", (prof.today() + timedelta(days=1)).isoformat())
    prof.add_assignment(session, owner, "CSE-3", "Later", (prof.today() + timedelta(days=5)).isoformat())
    msgs = prof.due_tomorrow_reminders(session)
    assert sorted(p for _, p, _ in msgs) == ["919811111111", "919833333333"]  # Neha has no phone
    assert "'Assignment 2' is due tomorrow" in msgs[0][2]
    assert prof.due_tomorrow_reminders(session) == []  # never twice


def say(session, text, *steps):
    return graph.handle_message(session, "922", text, fake_llm(*steps))


def test_remind_class_waits_for_yes(session, owner):
    say(session, "class ko bolo kal lab hai",
        [("remind_class", {"class_name": "CSE-3", "message": "Kal lab hai, 10 baje."})], "Bhejun?")
    assert count(session, PendingAction) == 1 and outbox.take(session) == []
    assert "Sent to 2 students" in say(session, "haan")
    sent = outbox.take(session)
    assert sorted(i["to"] for i in sent) == ["919811111111", "919833333333"]
    assert sent[0]["text"] == "Prof Rao (CSE-3): Kal lab hai, 10 baje."


def test_remind_class_without_phones_errors_at_once(session, owner):
    prof.add_class(session, owner, "ECE-1")
    say(session, "ECE ko message", [("remind_class", {"class_name": "ECE-1", "message": "Hi"})], "Kisi ka number nahi.")
    assert count(session, PendingAction) == 0


def test_reminder_job_writes_inbox_once(engine, monkeypatch):
    from contextlib import contextmanager

    from sqlalchemy.orm import Session

    from app import scheduler as sched
    from app.db.models import Notification

    with Session(engine) as s:
        p = User(phone="922", name="Prof Rao", role="professor")
        s.add(p)
        s.flush()
        prof.add_class(s, p.id, "CSE-3")
        prof.add_students(s, p.id, "CSE-3", [{"roll_no": 1, "name": "Ravi", "phone": "9811111111"}])
        accept_invites(s)
        prof.add_assignment(s, p.id, "CSE-3", "A2", (prof.today() + timedelta(days=1)).isoformat())
        s.commit()

    @contextmanager
    def test_session():
        with Session(engine) as s:
            yield s
            s.commit()

    monkeypatch.setattr(sched, "get_session", test_session)
    assert sched.send_assignment_reminders() == 1
    assert sched.send_assignment_reminders() == 0  # never twice
    with Session(engine) as s:
        texts = s.scalars(select(Notification.text).where(Notification.text.like("%due tomorrow%"))).all()
    assert texts == ["Reminder from Prof Rao (CSE-3): 'A2' is due tomorrow."]
