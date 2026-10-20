"""P2-2 checks: GST bills."""

from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import func, select

from app.agent import graph, outbox
from app.agent.roles import billing
from app.agent.roles import inventory as inv
from app.agent.roles import shopkeeper as shop
from app.config import get_settings
from app.db.models import Bill, BillItem, Product, User
from tests.conftest import fake_llm, needs_db

pytestmark = needs_db


@pytest.fixture(autouse=True)
def bills_in_tmp(tmp_path, monkeypatch):
    monkeypatch.setattr(get_settings(), "bills_dir", str(tmp_path))


@pytest.fixture
def owner(session):
    user = User(phone="911", name="Yash Store", role="shopkeeper", gstin="05ABCDE1234F1Z5")
    session.add(user)
    session.flush()
    inv.add_product(session, user.id, "Maggi", price="14", gst_rate="18", opening_stock="20")
    inv.add_product(session, user.id, "Atta", price="50", gst_rate="5", opening_stock="10", unit="kg")
    return user.id


def stock(session, name):
    return session.scalar(select(Product.stock).where(Product.name == name))


def count(session, model):
    return session.scalar(select(func.count()).select_from(model))


def test_gst_split_is_inclusive():
    assert billing.gst_split(Decimal("28.00"), Decimal("18")) == (Decimal("23.73"), Decimal("4.27"))
    assert billing.gst_split(Decimal("50.00"), Decimal("5")) == (Decimal("47.62"), Decimal("2.38"))
    assert billing.gst_split(Decimal("40.00"), Decimal("0")) == (Decimal("40.00"), Decimal("0.00"))


def test_cash_bill_totals_and_stock(session, owner):
    r = billing.create_bill(session, owner, [{"product_name": "maggi", "qty": "2"}, {"product_name": "Atta", "qty": "1"}])
    assert r["ok"] and r["bill_no"] == 1
    assert r["total"] == "78.00" and r["gst_total"] == "6.65"
    assert "Maggi 2 pcs × ₹14.00 = ₹28.00" in r["message"] and "Total ₹78.00 (incl. GST ₹6.65)" in r["message"]
    bill = session.scalar(select(Bill))
    assert bill.taxable + bill.gst_total == bill.total
    assert stock(session, "Maggi") == Decimal("18") and stock(session, "Atta") == Decimal("9")


def test_loose_quantity(session, owner):
    r = billing.create_bill(session, owner, [{"product_name": "Atta", "qty": "2.5"}])
    assert r["total"] == "125.00" and stock(session, "Atta") == Decimal("7.5")


def test_repeated_product_lines_are_merged(session, owner):
    billing.create_bill(session, owner, [{"product_name": "Maggi", "qty": "2"}, {"product_name": "maggi", "qty": "3"}])
    assert count(session, BillItem) == 1 and stock(session, "Maggi") == Decimal("15")


@pytest.mark.parametrize("items", [
    [{"product_name": "Maggi", "qty": "2"}, {"product_name": "Unknown thing", "qty": "1"}],  # missing product
    [{"product_name": "Maggi", "qty": "2"}, {"product_name": "Atta", "qty": "11"}],  # not enough stock
    [{"product_name": "Maggi", "qty": "2"}, {"product_name": "Atta", "qty": "-1"}],  # bad qty
    [{"product_name": "Maggi", "qty": "15"}, {"product_name": "Maggi", "qty": "10"}],  # merged lines exceed stock
    [], [{"product_name": "Maggi"}],
])
def test_bad_bill_changes_nothing(session, owner, items):
    r = billing.create_bill(session, owner, items)
    assert not r["ok"] and r["message"].startswith("Not saved")
    assert count(session, Bill) == 0
    assert stock(session, "Maggi") == Decimal("20") and stock(session, "Atta") == Decimal("10")


def test_credit_bill_goes_to_ledger(session, owner):
    r = billing.create_bill(session, owner, [{"product_name": "Atta", "qty": "2"}], customer_name="Sharma ji", on_credit=True)
    assert r["ok"] and "Added to Sharma's credit. New balance: ₹100.00." in r["message"]
    assert shop.get_balance(session, owner, "Sharma")["balance"] == "100.00"
    assert session.scalar(select(Bill.customer_id)) is not None


def test_credit_needs_a_customer(session, owner):
    assert not billing.create_bill(session, owner, [{"product_name": "Atta", "qty": "1"}], on_credit=True)["ok"]
    assert count(session, Bill) == 0


def test_cash_bill_for_unknown_customer_is_refused(session, owner):
    r = billing.create_bill(session, owner, [{"product_name": "Atta", "qty": "1"}], customer_name="Nobody")
    assert not r["ok"] and count(session, Bill) == 0


def test_pdf_made_and_queued_for_owner(session, owner):
    billing.create_bill(session, owner, [{"product_name": "Maggi", "qty": "1"}])
    [item] = outbox.take(session)
    assert item["to"] == "911" and item["kind"] == "document" and item["filename"] == "bill_1.pdf"
    pdf = Path(item["file"]).read_bytes()
    assert pdf.startswith(b"%PDF") and len(pdf) > 500


def test_pdf_failure_keeps_the_bill(session, owner, monkeypatch):
    monkeypatch.setattr(get_settings(), "bills_dir", "/proc/not-writable")
    r = billing.create_bill(session, owner, [{"product_name": "Maggi", "qty": "1"}])
    assert r["ok"] and "PDF could not be made" in r["message"] and count(session, Bill) == 1
    assert outbox.take(session) == []


def test_bill_numbers_per_shop(session, owner):
    other = User(phone="922", name="Other", role="shopkeeper")
    session.add(other)
    session.flush()
    inv.add_product(session, other.id, "Maggi", price="14", opening_stock="5")
    billing.create_bill(session, owner, [{"product_name": "Maggi", "qty": "1"}])
    billing.create_bill(session, owner, [{"product_name": "Maggi", "qty": "1"}])
    assert billing.create_bill(session, other.id, [{"product_name": "Maggi", "qty": "1"}])["bill_no"] == 1


def test_old_bill_keeps_old_price(session, owner):
    billing.create_bill(session, owner, [{"product_name": "Maggi", "qty": "1"}])
    inv.add_product(session, owner, "Maggi", price="15")
    assert session.scalar(select(BillItem.price)) == Decimal("14.00")


def test_bill_through_the_agent(session, owner):
    llm = fake_llm([("create_bill", {"items": [{"product_name": "Maggi", "qty": 2}], "customer_name": "Gupta",
                                     "on_credit": True})], "Bill ban gaya.")
    assert graph.handle_message(session, "911", "Gupta ke khate me 2 maggi", llm) == "Bill ban gaya."
    assert shop.get_balance(session, owner, "Gupta")["balance"] == "28.00"


def test_undo_never_half_reverses_a_bill(session, owner):
    shop.add_credit(session, owner, "Gupta", 100)
    billing.create_bill(session, owner, [{"product_name": "Atta", "qty": "2"}], customer_name="Sharma", on_credit=True)
    r = shop.undo_last_entry(session, owner)
    assert not r["ok"] and "Bill #1" in r["message"] and "cancel_bill" in r["message"]
    assert shop.get_balance(session, owner, "Sharma")["balance"] == "100.00"  # credit kept with its bill
    assert shop.get_balance(session, owner, "Gupta")["balance"] == "100.00"  # older credit not touched


def test_undo_after_bill_then_new_credit_reverses_the_credit(session, owner):
    billing.create_bill(session, owner, [{"product_name": "Maggi", "qty": "1"}])
    shop.add_credit(session, owner, "Gupta", 100)
    assert shop.undo_last_entry(session, owner)["balance"] == "0.00"


def test_pdf_with_markup_in_names(session, owner):
    billing.create_bill(session, owner, [{"product_name": "Maggi", "qty": "1"}], customer_name="A <b & C", on_credit=True)
    [item] = outbox.take(session)
    assert Path(item["file"]).read_bytes().startswith(b"%PDF")


def test_cancel_cash_bill_returns_stock(session, owner):
    billing.create_bill(session, owner, [{"product_name": "Maggi", "qty": "3"}, {"product_name": "Atta", "qty": "1"}])
    r = billing.cancel_bill(session, owner, "1")
    assert r["ok"] and "Stock returned" in r["message"]
    assert stock(session, "Maggi") == Decimal("20") and stock(session, "Atta") == Decimal("10")
    assert session.scalar(select(Bill.cancelled_at)) is not None and count(session, Bill) == 1  # kept, not deleted
    assert not billing.cancel_bill(session, owner, "#1")["ok"]  # never twice


def test_cancel_credit_bill_reverses_credit_only_once(session, owner):
    shop.add_credit(session, owner, "Sharma", 100)
    billing.create_bill(session, owner, [{"product_name": "Atta", "qty": "2"}], customer_name="Sharma", on_credit=True)
    r = billing.cancel_bill(session, owner, 1)
    assert "Sharma's credit reduced by ₹100.00; new balance ₹100.00." in r["message"]
    assert shop.get_balance(session, owner, "Sharma")["balance"] == "100.00"
    r = shop.undo_last_entry(session, owner)
    assert not r["ok"] and "cancellation of Bill #1" in r["message"]  # never reaches past it to the older credit
    assert shop.get_balance(session, owner, "Sharma")["balance"] == "100.00"


def test_cancel_after_payment_says_refund_is_due(session, owner):
    billing.create_bill(session, owner, [{"product_name": "Atta", "qty": "2"}], customer_name="Sharma", on_credit=True)
    shop.record_payment(session, owner, "Sharma", 100)
    r = billing.cancel_bill(session, owner, "1")
    assert "you owe them ₹100.00 back" in r["message"]


def test_cancelled_bill_leaves_the_summary(session, owner):
    from app.agent.roles.summary import summary_data

    billing.create_bill(session, owner, [{"product_name": "Atta", "qty": "2"}], customer_name="Sharma", on_credit=True)
    billing.cancel_bill(session, owner, "1")
    d = summary_data(session, owner)
    assert d["bills"] == 0 and d["sales"] == "0" and d["credit_given"] == "0" and d["total_dues"] == "0"


@pytest.mark.parametrize("bad", ["abc", "99", ""])
def test_cancel_bad_bill_numbers(session, owner, bad):
    assert not billing.cancel_bill(session, owner, bad)["ok"]
    assert not billing.needs_approval_cancel_bill(session, owner, {"bill_no": bad})


def test_cancel_waits_for_yes(session, owner):
    from app.db.models import PendingAction

    billing.create_bill(session, owner, [{"product_name": "Maggi", "qty": "2"}])
    graph.handle_message(session, "911", "bill 1 cancel karo", fake_llm([("cancel_bill", {"bill_no": "1"})], "Confirm?"))
    assert count(session, PendingAction) == 1 and stock(session, "Maggi") == Decimal("18")
    assert graph.handle_message(session, "911", "haan", fake_llm()).startswith("Cancelled Bill #1")
    assert stock(session, "Maggi") == Decimal("20")


def test_undo_after_bill_points_to_cancel(session, owner):
    billing.create_bill(session, owner, [{"product_name": "Maggi", "qty": "1"}])
    assert "cancel_bill" in shop.undo_last_entry(session, owner)["message"]

