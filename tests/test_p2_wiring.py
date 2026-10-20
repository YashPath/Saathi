"""P2-W checks: messaging tools behind the approval gate, through the agent."""

import pytest
from sqlalchemy import func, select

from app.agent import graph, outbox
from app.agent.roles import messaging as msg
from app.agent.roles import shopkeeper as shop
from app.agent.roles.registry import get_role
from app.config import get_settings
from app.db.models import PendingAction, User
from tests.conftest import fake_llm, needs_db

pytestmark = needs_db


@pytest.fixture(autouse=True)
def files_in_tmp(tmp_path, monkeypatch):
    monkeypatch.setattr(get_settings(), "bills_dir", str(tmp_path))


@pytest.fixture
def owner(session):
    user = User(phone="911", name="Yash Store", role="shopkeeper")
    session.add(user)
    session.flush()
    msg.add_supplier(session, user.id, "Ramesh Traders", "9876543210")
    return user.id


def pending(session):
    return session.scalar(select(func.count()).select_from(PendingAction))


def say(session, text, *steps):
    return graph.handle_message(session, "911", text, fake_llm(*steps))


ORDER = ("order_from_supplier", {"supplier_name": "Ramesh Traders", "items": [{"product_name": "Maggi", "qty": 24}]})


def test_shop_role_has_every_tool():
    names = {t.__name__ for t in get_role("shopkeeper").tools}
    assert {"add_credit", "create_bill", "add_stock", "order_from_supplier", "send_payment_reminder",
            "daily_summary"} <= names
    assert "add_credit" not in {t.__name__ for t in get_role("professor").tools}


def test_order_waits_then_yes_sends(session, owner):
    say(session, "Ramesh se 24 maggi mangwao", [ORDER], "Confirm karein?")
    assert pending(session) == 1 and outbox.take(session) == []
    reply = say(session, "haan")
    assert reply.startswith("Order sent to Ramesh Traders")
    [item] = outbox.take(session)
    assert item["to"] == "919876543210" and "Maggi: 24" in item["text"]


def test_order_no_sends_nothing(session, owner):
    say(session, "order", [ORDER], "Confirm?")
    assert say(session, "nahi") == "Cancelled. Nothing was saved."
    assert outbox.take(session) == []


def test_invalid_order_errors_without_pending(session, owner):
    bad = ("order_from_supplier", {"supplier_name": "Nobody", "items": [{"product_name": "Maggi", "qty": 1}]})
    say(session, "order", [bad], "Supplier nahi mila.")
    assert pending(session) == 0 and outbox.take(session) == []


def test_reminder_waits_then_yes_sends_text_and_qr(session, owner):
    msg.set_shop_details(session, owner, gstin="05ABCDE1234F1Z5")  # GSTIN alone needs no approval
    session.get(User, owner).upi_id = "yash@okaxis"
    shop.add_credit(session, owner, "Sharma", 300)
    msg.set_customer_phone(session, owner, "Sharma", "9811122233")
    say(session, "Sharma ko yaad dilao", [("send_payment_reminder", {"customer_name": "Sharma"})], "Bhejun?")
    assert pending(session) == 1 and outbox.take(session) == []
    say(session, "yes")
    kinds = [i.get("kind", "text") for i in outbox.take(session)]
    assert kinds == ["text", "image"]


def test_upi_change_waits_for_yes(session, owner):
    say(session, "UPI badlo", [("set_shop_details", {"upi_id": "new@okaxis"})], "Confirm?")
    assert session.get(User, owner).upi_id is None and pending(session) == 1
    say(session, "haan")
    assert session.get(User, owner).upi_id == "new@okaxis"


def test_gstin_alone_saves_at_once(session, owner):
    say(session, "GSTIN", [("set_shop_details", {"gstin": "05ABCDE1234F1Z5"})], "Saved.")
    assert session.get(User, owner).gstin == "05ABCDE1234F1Z5" and pending(session) == 0


def test_big_payment_still_gated(session, owner):
    shop.add_credit(session, owner, "Gupta", 9000)
    say(session, "Gupta ne 6000 diye", [("record_payment", {"customer_name": "Gupta", "amount": "6000"})], "Confirm?")
    assert pending(session) == 1


def test_same_upi_again_needs_no_approval(session, owner):
    session.get(User, owner).upi_id = "yash@okaxis"
    say(session, "UPI", [("set_shop_details", {"upi_id": "yash@okaxis"})], "Saved.")
    assert pending(session) == 0


def test_refused_at_yes_is_logged_as_rejected(session, owner):
    from app.agent.approval import REQUEST_TOOL
    from app.db.models import ActionLog

    say(session, "order", [ORDER], "Confirm?")
    from app.db.models import Supplier
    session.delete(session.scalar(select(Supplier)))  # supplier removed before the owner says yes
    session.flush()
    assert "Not sent" in say(session, "haan")
    assert session.scalar(select(ActionLog.status).where(ActionLog.tool_name == REQUEST_TOOL)) == "rejected"
    assert outbox.take(session) == []
