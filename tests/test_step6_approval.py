"""Step 6 checks: approval gate for big payments."""

from datetime import timedelta

import pytest
from sqlalchemy import func, select

from app.agent import approval, graph
from app.agent.roles import shopkeeper as shop
from app.db.models import ActionLog, LedgerEntry, PendingAction, User
from tests.conftest import fake_llm, needs_db

pytestmark = needs_db


def count(session, model):
    return session.scalar(select(func.count()).select_from(model))


@pytest.fixture
def owner(session):
    user = User(phone="911", name="Shop", role="shopkeeper")
    session.add(user)
    session.flush()
    shop.add_credit(session, user.id, "Gupta", 9000)
    return user.id


def ask_big_payment(session, amount="6000"):
    llm = fake_llm([("record_payment", {"customer_name": "Gupta", "amount": amount})], "Please confirm.")
    return graph.handle_message(session, "911", f"Gupta ne {amount} diye", llm)


def test_big_payment_waits(session, owner):
    ask_big_payment(session)
    assert count(session, PendingAction) == 1
    assert count(session, LedgerEntry) == 1  # only the setup credit
    assert shop.get_balance(session, owner, "Gupta")["balance"] == "9000.00"


def test_yes_executes(session, owner):
    ask_big_payment(session)
    reply = graph.handle_message(session, "911", "Haan!", fake_llm())  # no LLM call needed
    assert reply == "Recorded ₹6,000.00 payment from Gupta. New balance: ₹3,000.00"
    assert count(session, PendingAction) == 0
    req = session.scalar(select(ActionLog).where(ActionLog.tool_name == approval.REQUEST_TOOL))
    assert req.status == "done"


def test_no_rejects(session, owner):
    ask_big_payment(session)
    assert graph.handle_message(session, "911", "nahi", fake_llm()) == "Cancelled. Nothing was saved."
    assert count(session, LedgerEntry) == 1 and count(session, PendingAction) == 0
    assert session.scalar(select(ActionLog.status).where(ActionLog.tool_name == approval.REQUEST_TOOL)) == "rejected"


def test_expired_yes_does_nothing(session, owner):
    ask_big_payment(session)
    pending = session.scalar(select(PendingAction))
    pending.expires_at = approval.now() - timedelta(minutes=1)
    session.flush()
    assert "expired" in graph.handle_message(session, "911", "yes", fake_llm())
    assert count(session, LedgerEntry) == 1


def test_small_payment_runs_directly(session, owner):
    ask_big_payment(session, amount="5000")
    assert count(session, PendingAction) == 0
    assert shop.get_balance(session, owner, "Gupta")["balance"] == "4000.00"


def test_new_request_after_moving_on(session, owner):
    ask_big_payment(session, "6000")
    ask_big_payment(session, "7000")
    assert count(session, PendingAction) == 1
    graph.handle_message(session, "911", "yes", fake_llm())
    assert shop.get_balance(session, owner, "Gupta")["balance"] == "2000.00"


def test_undo_ignores_approval_logs(session, owner):
    ask_big_payment(session)
    graph.handle_message(session, "911", "yes", fake_llm())
    assert shop.undo_last_entry(session, owner)["balance"] == "9000.00"


def test_yes_without_pending_goes_to_llm(session, owner):
    assert graph.handle_message(session, "911", "ok", fake_llm("Kya karna hai?")) == "Kya karna hai?"


def test_two_yes_at_once_records_once(engine):
    import threading
    import time

    from sqlalchemy.orm import Session

    with Session(engine) as s:
        user = User(phone="911", name="Shop", role="shopkeeper")
        s.add(user)
        s.flush()
        shop.add_credit(s, user.id, "Gupta", 9000)
        ask_big_payment(s)
        s.commit()
    with Session(engine) as first, Session(engine) as second:
        graph.handle_message(first, "911", "yes", fake_llm())  # not committed yet, like a slow webhook worker
        t = threading.Thread(target=graph.handle_message, args=(second, "911", "yes", fake_llm("Kuch aur?")))
        t.start()
        time.sleep(0.5)
        first.commit()
        t.join(10)
        second.commit()
    with Session(engine) as s:
        assert count(s, LedgerEntry) == 2  # setup credit + ONE payment


def test_two_big_payments_in_one_message(session, owner):
    shop.add_credit(session, owner, "Sharma", 9000)
    llm = fake_llm([("record_payment", {"customer_name": "Gupta", "amount": "6000"}),
                    ("record_payment", {"customer_name": "Sharma", "amount": "7000"})], "Please confirm.")
    graph.handle_message(session, "911", "Gupta 6000, Sharma 7000 diye", llm)
    assert count(session, PendingAction) == 1
    graph.handle_message(session, "911", "yes", fake_llm())
    assert shop.get_balance(session, owner, "Gupta")["balance"] == "3000.00"  # first one confirmed
    assert shop.get_balance(session, owner, "Sharma")["balance"] == "9000.00"  # second refused, not silently lost


def test_old_payment_not_confirmed_by_later_yes(session, owner):
    ask_big_payment(session)
    reply = graph.handle_message(session, "911", "Ramu ko add karna hai?", fake_llm("Ramu ka kitna likhun?"))
    assert count(session, PendingAction) == 0  # moving on cancels it...
    assert "approval cancelled: record payment" in reply  # ...and the owner is told
    graph.handle_message(session, "911", "haan", fake_llm("Kitna?"))
    assert shop.get_balance(session, owner, "Gupta")["balance"] == "9000.00"


@pytest.mark.parametrize("answer", ["haan kar do", "Yes please", "👍", "theek hai", "ok!"])
def test_natural_yes_confirms(session, owner, answer):
    ask_big_payment(session)
    graph.handle_message(session, "911", answer, fake_llm())
    assert shop.get_balance(session, owner, "Gupta")["balance"] == "3000.00"


@pytest.mark.parametrize("answer", ["nahi yaar", "No thanks", "👎", "rehne do"])
def test_natural_no_cancels(session, owner, answer):
    ask_big_payment(session)
    assert graph.handle_message(session, "911", answer, fake_llm()) == "Cancelled. Nothing was saved."


def test_long_message_is_a_new_request_not_an_answer(session, owner):
    ask_big_payment(session)
    graph.handle_message(session, "911", "haan, Gupta ka balance batao", fake_llm("Balance ₹9,000"))
    assert shop.get_balance(session, owner, "Gupta")["balance"] == "9000.00"


@pytest.mark.parametrize("text", ["ok Gupta 500", "karo Gupta 500", "done Sharma 200", "👍 Gupta ko 9000 likh do"])
def test_short_new_request_with_amount_is_not_a_yes(session, owner, text):
    ask_big_payment(session)
    graph.handle_message(session, "911", text, fake_llm("Kis customer ka?"))
    assert shop.get_balance(session, owner, "Gupta")["balance"] == "9000.00"  # old 6000 payment NOT confirmed
