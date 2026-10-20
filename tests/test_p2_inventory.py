"""P2-1 checks: inventory tools and outbox delivery."""

from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.agent import graph, outbox
from app.agent.roles import inventory as inv
from app.db.models import Product, User
from tests.conftest import fake_llm, needs_db

pytestmark = needs_db


@pytest.fixture
def owner(session):
    user = User(phone="911", name="Shop", role="shopkeeper")
    session.add(user)
    session.flush()
    return user.id


def count(session):
    return session.scalar(select(func.count()).select_from(Product))


def test_add_product_and_stock(session, owner):
    r = inv.add_product(session, owner, "maggi", price="14", gst_rate="18", opening_stock="24", reorder_level="10")
    assert r["message"] == "Added Maggi: 24 pcs in stock, ₹14.00 each (GST 18%)."
    assert inv.add_stock(session, owner, "Maggi", 12)["message"] == "Added 12 pcs of Maggi. Now 36 in stock."


def test_loose_items_in_kg(session, owner):
    inv.add_product(session, owner, "Chini", price="45", unit="KG", opening_stock="2.5")
    inv.add_stock(session, owner, "chini", "0.75")
    assert inv.check_stock(session, owner, "Chini")["message"] == "Chini: 3.25 kg in stock, ₹45.00 each (GST 0%)."


def test_update_fields_including_gst_to_zero(session, owner):
    inv.add_product(session, owner, "Atta", price="50", gst_rate="5")
    r = inv.add_product(session, owner, "atta", price="55", gst_rate="0")
    assert r["message"].startswith("Updated Atta") and "₹55.00" in r["message"] and "GST 0%" in r["message"]
    assert count(session) == 1


@pytest.mark.parametrize("kwargs", [{"price": "0"}, {"price": "abc"}, {"price": "10", "gst_rate": "12"},
                                    {"price": "10", "gst_rate": "28"}, {"price": "10", "opening_stock": "-1"},
                                    {"price": "10", "opening_stock": "1.2345"}])
def test_invalid_values_save_nothing(session, owner, kwargs):
    assert not inv.add_product(session, owner, "Soap", **kwargs)["ok"]
    assert count(session) == 0


def test_new_product_needs_price(session, owner):
    r = inv.add_product(session, owner, "Bread")
    assert not r["ok"] and "price" in r["message"] and count(session) == 0


def test_opening_stock_on_existing_product_is_refused(session, owner):
    inv.add_product(session, owner, "Bread", price="40", opening_stock="5")
    assert not inv.add_product(session, owner, "Bread", opening_stock="10")["ok"]
    assert session.scalar(select(Product.stock)) == Decimal("5")


def test_product_names_are_not_trimmed_like_people(session, owner):
    inv.add_product(session, owner, "Sabji Masala", price="30")
    assert session.scalar(select(Product.name)) == "Sabji Masala"


def test_similar_product_asks(session, owner):
    inv.add_product(session, owner, "Parle G", price="10")
    inv.add_product(session, owner, "Parle Monaco", price="20")
    r = inv.add_stock(session, owner, "Parle", 5)
    assert not r["ok"] and r["ambiguous"] == ["Parle G", "Parle Monaco"]


def test_low_stock_list(session, owner):
    inv.add_product(session, owner, "Maggi", price="14", opening_stock="5", reorder_level="10")
    inv.add_product(session, owner, "Atta", price="50", opening_stock="20", reorder_level="10")
    inv.add_product(session, owner, "Salt", price="20", opening_stock="0")  # no reorder level set
    r = inv.check_stock(session, owner)
    assert r["low_stock"] == ["Maggi"]


def test_products_are_per_shop(session, owner):
    other = User(phone="922", name="Other", role="shopkeeper")
    session.add(other)
    session.flush()
    inv.add_product(session, owner, "Maggi", price="14")
    assert not inv.check_stock(session, other.id, "Maggi")["ok"]
    assert not inv.add_stock(session, other.id, "Maggi", 5)["ok"]


def test_outbox_cleared_when_agent_fails(session, owner):
    outbox.queue_text(session, "999", "should never be sent")
    llm = fake_llm([("add_stock", {"product_name": "X", "qty": "1"})])  # runs out of script -> raises
    assert graph.handle_message(session, "911", "x", llm) == graph.FAILED
    assert outbox.take(session) == []
