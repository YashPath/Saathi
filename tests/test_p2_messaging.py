"""P2-3/4 checks: supplier orders, payment reminders, shop details."""

from datetime import timedelta
from urllib.parse import parse_qs, urlparse

import pytest
from sqlalchemy import select

from app.agent import outbox
from app.agent.roles import inventory as inv
from app.agent.roles import messaging as msg
from app.agent.roles import shopkeeper as shop
from app.config import get_settings
from app.db.models import ActionLog, User
from tests.conftest import needs_db


@pytest.fixture(autouse=True)
def files_in_tmp(tmp_path, monkeypatch):
    monkeypatch.setattr(get_settings(), "bills_dir", str(tmp_path))


@pytest.fixture
def owner(session):
    user = User(phone="911", name="Yash Store", role="shopkeeper")
    session.add(user)
    session.flush()
    return user.id


@pytest.mark.parametrize("raw,expected", [
    ("9876543210", "919876543210"), ("+91 98765-43210", "919876543210"), ("09876543210", "919876543210"),
    ("919876543210", "919876543210"), ("+1 415 555 0100", "14155550100"),
    ("12345", None), ("5876543210", None), ("abc", None), ("", None), ("0012345678901", None),
])
def test_parse_phone(raw, expected):
    assert msg.parse_phone(raw) == expected


@needs_db
class TestDb:
    def test_order_queues_text_to_supplier(self, session, owner):
        inv.add_product(session, owner, "Maggi", price="14")
        msg.add_supplier(session, owner, "Ramesh Traders", "98765 43210")
        r = msg.order_from_supplier(session, owner, "ramesh traders",
                                    [{"product_name": "maggi", "qty": 24}, {"product_name": "Sugar", "qty": "10", "unit": "kg"}])
        assert r["ok"]
        [item] = outbox.take(session)
        assert item["to"] == "919876543210"
        assert "- Maggi: 24 pcs" in item["text"] and "- Sugar: 10 kg" in item["text"] and "Yash Store" in item["text"]

    @pytest.mark.parametrize("supplier,items", [
        ("Nobody", [{"product_name": "Maggi", "qty": "1"}]),
        ("Ramesh Traders", []),
        ("Ramesh Traders", [{"product_name": "Maggi", "qty": "0"}]),
        ("Ramesh Traders", [{"product_name": "Maggi"}]),
    ])
    def test_bad_order_sends_nothing_and_needs_no_approval(self, session, owner, supplier, items):
        msg.add_supplier(session, owner, "Ramesh Traders", "9876543210")
        args = {"supplier_name": supplier, "items": items}
        assert not msg.needs_approval_order(session, owner, args)
        assert not msg.order_from_supplier(session, owner, **args)["ok"]
        assert outbox.take(session) == []

    def test_valid_order_always_needs_approval(self, session, owner):
        msg.add_supplier(session, owner, "Ramesh Traders", "9876543210")
        assert msg.needs_approval_order(session, owner, {"supplier_name": "Ramesh Traders",
                                                          "items": [{"product_name": "Maggi", "qty": "5"}]})

    def test_reminder_with_upi_link_and_qr(self, session, owner):
        msg.set_shop_details(session, owner, upi_id="yashstore@okaxis")
        shop.add_credit(session, owner, "Sharma", 450)
        msg.set_customer_phone(session, owner, "Sharma ji", "9811122233")
        r = msg.send_payment_reminder(session, owner, "Sharma")
        assert r["ok"] and "₹450.00" in r["message"]
        text, qr = outbox.take(session)
        assert text["to"] == qr["to"] == "919811122233" and qr["kind"] == "image"
        link = text["text"].split("Pay: ")[1]
        q = parse_qs(urlparse(link).query)
        assert link.startswith("upi://pay?") and q["pa"] == ["yashstore@okaxis"] and q["am"] == ["450.00"]
        assert q["cu"] == ["INR"]

    def test_reminder_without_upi_still_sends_text(self, session, owner):
        shop.add_credit(session, owner, "Sharma", 100)
        msg.set_customer_phone(session, owner, "Sharma", "9811122233")
        r = msg.send_payment_reminder(session, owner, "Sharma")
        assert "set your UPI ID" in r["message"]
        [item] = outbox.take(session)
        assert "upi://" not in item["text"]

    @pytest.mark.parametrize("setup,expected", [
        ("no_phone", "don't have"), ("no_dues", "nothing pending"), ("unknown", "No customer"),
    ])
    def test_reminder_refusals(self, session, owner, setup, expected):
        if setup != "unknown":
            shop.add_credit(session, owner, "Sharma", 100)
        if setup == "no_dues":
            msg.set_customer_phone(session, owner, "Sharma", "9811122233")
            shop.record_payment(session, owner, "Sharma", 100)
        name = "Sharma" if setup != "unknown" else "Nobody"
        assert not msg.needs_approval_reminder(session, owner, {"customer_name": name})
        r = msg.send_payment_reminder(session, owner, name)
        assert not r["ok"] and expected in r["message"] and outbox.take(session) == []

    def test_one_reminder_per_day(self, session, owner):
        shop.add_credit(session, owner, "Sharma", 100)
        msg.set_customer_phone(session, owner, "Sharma", "9811122233")
        assert msg.send_payment_reminder(session, owner, "Sharma")["ok"]
        outbox.take(session)
        assert "already reminded" in msg.send_payment_reminder(session, owner, "Sharma")["message"]
        log = session.scalar(select(ActionLog).where(ActionLog.tool_name == "send_payment_reminder"))
        log.created_at = log.created_at - timedelta(hours=25)
        session.flush()
        assert msg.send_payment_reminder(session, owner, "Sharma")["ok"]

    @pytest.mark.parametrize("kwargs,ok", [
        ({"upi_id": "yash@okaxis"}, True), ({"upi_id": "not a upi"}, False), ({"upi_id": "@okaxis"}, False),
        ({"gstin": "05abcde1234f1z5"}, True), ({"gstin": "12345"}, False), ({}, False),
    ])
    def test_shop_details_validation(self, session, owner, kwargs, ok):
        assert msg.set_shop_details(session, owner, **kwargs)["ok"] is ok
        if ok and "gstin" in kwargs:
            assert session.get(User, owner).gstin == "05ABCDE1234F1Z5"

    def test_only_upi_change_needs_approval(self, session, owner):
        assert msg.needs_approval_shop_details(session, owner, {"upi_id": "yash@okaxis"})
        assert not msg.needs_approval_shop_details(session, owner, {"gstin": "05ABCDE1234F1Z5"})
        assert not msg.needs_approval_shop_details(session, owner, {"upi_id": "bad"})

    def test_suppliers_and_customers_are_per_shop(self, session, owner):
        other = User(phone="922", name="Other", role="shopkeeper")
        session.add(other)
        session.flush()
        msg.add_supplier(session, owner, "Ramesh Traders", "9876543210")
        shop.add_credit(session, owner, "Sharma", 100)
        assert not msg.order_from_supplier(session, other.id, "Ramesh Traders", [{"product_name": "X", "qty": "1"}])["ok"]
        assert not msg.set_customer_phone(session, other.id, "Sharma", "9811122233")["ok"]
