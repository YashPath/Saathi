"""P3-1 checks: classes, students, attendance."""

from datetime import timedelta

import pytest
from sqlalchemy import func, select

from app.agent.roles import professor as prof
from app.db.models import Attendance, Student, User
from tests.conftest import needs_db

pytestmark = needs_db


@pytest.fixture
def owner(session):
    user = User(phone="922", name="Prof Rao", role="professor")
    session.add(user)
    session.flush()
    prof.add_class(session, user.id, "CSE-3")
    prof.add_students(session, user.id, "cse-3", [
        {"roll_no": 1, "name": "ravi kumar", "phone": "9811111111"},
        {"roll_no": 2, "name": "Neha", "phone": "9822222222"},
        {"roll_no": 3, "name": "Aman"},
    ])
    return user.id


def count(session, model):
    return session.scalar(select(func.count()).select_from(model))


def test_students_with_phones_get_student_logins(session, owner):
    users = session.scalars(select(User.phone).where(User.role == "student")).all()
    assert sorted(users) == ["919811111111", "919822222222"]
    assert session.scalar(select(Student.name).where(Student.roll_no == 1)) == "Ravi Kumar"


def test_update_by_roll_number(session, owner):
    r = prof.add_students(session, owner, "CSE-3", [{"roll_no": 3, "name": "Aman Verma", "phone": "9833333333"},
                                                    {"roll_no": 4, "name": "Zoya"}])
    assert "1 added, 1 updated. Total 4 students." in r["message"]
    assert session.scalar(select(Student.phone).where(Student.roll_no == 3)) == "919833333333"


def test_phone_of_another_role_is_not_linked(session, owner):
    session.add(User(phone="919844444444", name="Shop", role="shopkeeper"))
    session.flush()
    r = prof.add_students(session, owner, "CSE-3", [{"roll_no": 5, "name": "Kabir", "phone": "9844444444"}])
    assert r["ok"] and "already used by a shopkeeper" in r["message"]
    assert session.scalar(select(Student.user_id).where(Student.roll_no == 5)) is None


@pytest.mark.parametrize("students", [[], [{"roll_no": 1, "name": "A"}, {"roll_no": 1, "name": "B"}],
                                      [{"roll_no": 0, "name": "A"}], [{"roll_no": 7, "name": "A", "phone": "123"}],
                                      [{"name": "No roll"}]])
def test_bad_student_lists_change_nothing(session, owner, students):
    before = count(session, Student)
    assert not prof.add_students(session, owner, "CSE-3", students)["ok"]
    assert count(session, Student) == before


def test_mark_and_remark_attendance(session, owner):
    r = prof.mark_attendance(session, owner, "CSE-3", [2])
    assert "2 present, 1 absent (2 Neha)" in r["message"]
    r = prof.mark_attendance(session, owner, "CSE-3", [])  # correction: everyone was there
    assert r["message"].startswith("Updated") and count(session, Attendance) == 3


def test_unknown_roll_refuses_everything(session, owner):
    assert not prof.mark_attendance(session, owner, "CSE-3", [2, 99])["ok"]
    assert count(session, Attendance) == 0


@pytest.mark.parametrize("day", ["tomorrow", "2026-13-01", "kal"])
def test_bad_or_future_dates(session, owner, day):
    assert not prof.mark_attendance(session, owner, "CSE-3", [], date=day)["ok"]


def test_future_iso_date_refused(session, owner):
    future = (prof.today() + timedelta(days=1)).isoformat()
    assert not prof.mark_attendance(session, owner, "CSE-3", [], date=future)["ok"]


def test_attendance_report(session, owner):
    yesterday = (prof.today() - timedelta(days=1)).isoformat()
    prof.mark_attendance(session, owner, "CSE-3", [2, 3], date=yesterday)
    prof.mark_attendance(session, owner, "CSE-3", [2])
    r = prof.attendance_report(session, owner, "CSE-3")
    assert r["below"] == [2, 3]  # Neha 0%, Aman 50%
    assert "overall 50.0%" in r["message"] and "2 Neha: 0.0% (0/2)" in r["message"]


def test_professors_only_see_their_classes(session, owner):
    other = User(phone="933", name="Prof B", role="professor")
    session.add(other)
    session.flush()
    assert not prof.mark_attendance(session, other.id, "CSE-3", [])["ok"]
    assert not prof.attendance_report(session, other.id, "CSE-3")["ok"]
    assert prof.add_class(session, other.id, "CSE-3")["ok"]  # same name in another professor's list is fine


def test_duplicate_class_refused(session, owner):
    assert not prof.add_class(session, owner, "cse-3")["ok"]


# --- consent ------------------------------------------------------------------

from app.agent import outbox  # noqa: E402
from app.agent.roles import student as stu  # noqa: E402


def ravi_id(session):
    return session.scalar(select(User.id).where(User.phone == "919811111111"))


def test_invite_sent_and_nothing_visible_before_joining(session, owner):
    invites = outbox.take(session)
    assert sorted(i["to"] for i in invites) == ["919811111111", "919822222222"]
    assert 'Say "join CSE-3"' in invites[0]["text"] and "Prof Rao" in invites[0]["text"]
    r = stu.my_attendance(session, ravi_id(session))
    assert "not joined any class" in r["message"] and "invited to: CSE-3" in r["message"]


def test_join_then_see_attendance(session, owner):
    prof.mark_attendance(session, owner, "CSE-3", [])
    assert stu.join_class(session, ravi_id(session), "cse-3")["ok"]
    assert stu.my_attendance(session, ravi_id(session))["message"].startswith("CSE-3: 100.0%")


def test_cannot_join_without_invite(session, owner):
    other = User(phone="933", name="Prof B", role="professor")
    session.add(other)
    session.flush()
    prof.add_class(session, other.id, "SECRET-1")
    assert not stu.join_class(session, ravi_id(session), "SECRET-1")["ok"]


def test_unjoined_students_get_no_class_messages(session, owner):
    outbox.take(session)
    r = prof.remind_class(session, owner, "CSE-3", "Kal lab hai")
    assert not r["ok"] and "joined" in r["message"] and outbox.take(session) == []


def test_new_number_means_new_invite_and_old_login_loses_access(session, owner):
    stu.join_class(session, ravi_id(session), "CSE-3")
    old = ravi_id(session)
    outbox.take(session)
    prof.add_students(session, owner, "CSE-3", [{"roll_no": 1, "name": "Ravi", "phone": "9899999999"}])
    assert [i["to"] for i in outbox.take(session)] == ["919899999999"]
    assert "not joined any class" in stu.my_attendance(session, old)["message"]


def test_same_number_again_does_not_reinvite_or_unjoin(session, owner):
    stu.join_class(session, ravi_id(session), "CSE-3")
    outbox.take(session)
    prof.add_students(session, owner, "CSE-3", [{"roll_no": 1, "name": "Ravi Kumar", "phone": "9811111111"}])
    assert outbox.take(session) == [] and "CSE-3" in stu.my_attendance(session, ravi_id(session))["message"]


def test_phone_can_be_removed(session, owner):
    stu.join_class(session, ravi_id(session), "CSE-3")
    prof.add_students(session, owner, "CSE-3", [{"roll_no": 1, "name": "Ravi", "phone": "none"}])
    s = session.scalar(select(Student).where(Student.roll_no == 1))
    assert s.phone is None and s.user_id is None and s.joined_at is None


def test_same_phone_on_two_rolls_warns(session, owner):
    r = prof.add_students(session, owner, "CSE-3", [{"roll_no": 7, "name": "Asha", "phone": "9811111111"}])
    assert "same number is also saved for roll 1 (Ravi Kumar)" in r["message"]


def test_invite_names_the_student(session, owner):
    invites = outbox.take(session)
    assert any("as roll 1, Ravi Kumar" in i["text"] for i in invites)
