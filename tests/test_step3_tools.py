"""Step 3 checks: shopkeeper ledger tools."""

from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.agent.roles import shopkeeper as shop
from app.db.models import ActionLog, Customer, LedgerEntry, User
from tests.conftest import needs_db

pytestmark = needs_db


def count(session, model):
    return session.scalar(select(func.count()).select_from(model))


@pytest.fixture
def owner(session):
    user = User(phone="919000000001", name="Shop A", role="shopkeeper")
    session.add(user)
    session.flush()
    return user.id


@pytest.fixture
def other_owner(session):
    user = User(phone="919000000002", name="Shop B", role="shopkeeper")
    session.add(user)
    session.flush()
    return user.id


def test_balance_math(session, owner):
    shop.add_credit(session, owner, "Sharma", 500)
    shop.add_credit(session, owner, "Sharma", "200")
    r = shop.record_payment(session, owner, "Sharma", 300)
    assert r["ok"] and r["balance"] == "400.00"
    assert r["message"] == "Recorded ₹300.00 payment from Sharma. New balance: ₹400.00"
    assert shop.get_balance(session, owner, "Sharma")["balance"] == "400.00"


def test_name_variants_hit_same_customer(session, owner):
    first = shop.add_credit(session, owner, "Sharma ji", 100)
    assert first["message"] == "Added ₹100.00 credit to Sharma. New balance: ₹100.00"
    shop.add_credit(session, owner, "sharmaji", 100)
    shop.add_credit(session, owner, "SHARMA", 100)
    shop.add_credit(session, owner, "Sharmaa", 100)  # typo, fuzzy match
    assert count(session, Customer) == 1
    assert shop.get_balance(session, owner, "sharma ji ka")["balance"] == "400.00"


def test_ambiguous_names(session, owner):
    shop.add_credit(session, owner, "Sharmaa Kirana", 100)
    shop.add_credit(session, owner, "Sharma Dairy", 100)
    r = shop.get_balance(session, owner, "Sharma")
    assert not r["ok"] and r["ambiguous"] == ["Sharma Dairy", "Sharmaa Kirana"]
    assert shop.add_credit(session, owner, "Sharma", 50)["ambiguous"]


def test_short_name_does_not_land_on_longer_one(session, owner):
    shop.add_credit(session, owner, "Ramesh", 100)
    shop.add_credit(session, owner, "Ram", 50)
    assert count(session, Customer) == 2
    assert shop.get_balance(session, owner, "Ramesh")["balance"] == "100.00"


@pytest.mark.parametrize("bad", [0, -5, "12.345", "abc", "NaN", None, 2_000_000])
def test_invalid_amounts_write_nothing(session, owner, bad):
    assert not shop.add_credit(session, owner, "Gupta", bad)["ok"]
    assert count(session, LedgerEntry) == 0 and count(session, ActionLog) == 0


def test_payment_for_unknown_customer(session, owner):
    r = shop.record_payment(session, owner, "Nobody", 10)
    assert not r["ok"] and "No customer" in r["message"]


def test_undo_twice_then_nothing(session, owner):
    shop.add_credit(session, owner, "Gupta", 500)
    shop.record_payment(session, owner, "Gupta", 200)
    assert shop.undo_last_entry(session, owner)["balance"] == "500.00"  # payment reversed
    assert shop.undo_last_entry(session, owner)["balance"] == "0.00"  # credit reversed
    assert not shop.undo_last_entry(session, owner)["ok"]
    assert count(session, LedgerEntry) == 4  # nothing deleted


def test_list_dues_sorted_and_positive_only(session, owner):
    shop.add_credit(session, owner, "Gupta", 100)
    shop.add_credit(session, owner, "Verma", 900)
    shop.add_credit(session, owner, "Khan", 300)
    shop.record_payment(session, owner, "Khan", 300)  # zero balance
    dues = shop.list_dues(session, owner)["dues"]
    assert dues == [{"customer": "Verma", "balance": "900.00"}, {"customer": "Gupta", "balance": "100.00"}]


def test_owners_are_isolated(session, owner, other_owner):
    shop.add_credit(session, owner, "Sharma", 500)
    assert not shop.get_balance(session, other_owner, "Sharma")["ok"]
    assert not shop.record_payment(session, other_owner, "Sharma", 100)["ok"]
    assert shop.list_dues(session, other_owner)["dues"] == []
    assert not shop.undo_last_entry(session, other_owner)["ok"]
    assert shop.get_balance(session, owner, "Sharma")["balance"] == "500.00"


def test_every_successful_call_is_logged(session, owner):
    shop.add_credit(session, owner, "Gupta", 100)
    shop.record_payment(session, owner, "Gupta", 50)
    shop.get_balance(session, owner, "Gupta")
    shop.list_dues(session, owner)
    shop.undo_last_entry(session, owner)
    tools = session.scalars(select(ActionLog.tool_name).order_by(ActionLog.id)).all()
    assert tools == ["add_credit", "record_payment", "get_balance", "list_dues", "undo_last_entry"]


@pytest.mark.parametrize("text,expected", [("₹500", "500.00"), ("500 rupees", "500.00"), ("Rs. 1,200", "1200.00"),
                                           ("500rs", "500.00"), (500, "500.00"), ("₹", None),
                                           ("500rs.50", None), ("1rs2", None)])  # never glue digits together
def test_amount_with_currency_words(text, expected):
    result = shop.parse_amount(text)
    assert (str(result) if result is not None else None) == expected


@pytest.mark.parametrize("existing,new", [("Sunil Kumar", "Anil Kumar"), ("Raju", "Raj")])
def test_similar_name_asks_then_can_be_a_new_person(session, owner, existing, new):
    shop.add_credit(session, owner, existing, 100)
    r = shop.add_credit(session, owner, new, 500)
    assert not r["ok"] and r["ambiguous"] == [existing]  # asks, never guesses
    session.commit()  # the owner's answer arrives as a new message (new transaction)
    assert shop.add_credit(session, owner, new, 500, new_customer=True)["ok"]
    assert count(session, Customer) == 2
    assert shop.get_balance(session, owner, existing)["balance"] == "100.00"


def test_typo_name_does_not_split_a_balance(session, owner):
    shop.add_credit(session, owner, "Gupta", 100)
    r = shop.add_credit(session, owner, "Gupt", 50)
    assert r["ambiguous"] == ["Gupta"]
    assert count(session, Customer) == 1 and count(session, LedgerEntry) == 1


def test_new_customer_flag_reuses_exact_name(session, owner):
    shop.add_credit(session, owner, "Gupta", 100)
    shop.add_credit(session, owner, "gupta ji", 50, new_customer=True)
    assert count(session, Customer) == 1


def test_two_undo_at_once_reverse_once(engine):
    import threading
    import time

    from sqlalchemy.orm import Session

    with Session(engine) as s:
        user = User(phone="911", name="Shop", role="shopkeeper")
        s.add(user)
        s.flush()
        shop.add_credit(s, user.id, "Gupta", 100)
        shop.add_credit(s, user.id, "Gupta", 200)
        s.commit()
        uid = user.id
    with Session(engine) as first, Session(engine) as second:
        shop.undo_last_entry(first, uid)  # not committed yet
        t = threading.Thread(target=shop.undo_last_entry, args=(second, uid))
        t.start()
        time.sleep(0.5)
        first.commit()
        t.join(10)
        second.commit()
    with Session(engine) as s:
        # each undo reversed a DIFFERENT entry: 100 + 200 - 200 - 100
        assert shop.get_balance(s, uid, "Gupta")["balance"] == "0.00"


def test_similar_name_on_payment_never_suggests_add_credit(session, owner):
    shop.add_credit(session, owner, "Gupta", 100)
    r = shop.record_payment(session, owner, "Gupt", 50)
    assert r["ambiguous"] == ["Gupta"] and "add_credit" not in r["message"]  # would turn a payment into a credit


def test_new_customer_flag_ignored_unless_we_asked(session, owner):
    shop.add_credit(session, owner, "Gupta", 100)
    r = shop.add_credit(session, owner, "Gupt", 50, new_customer=True)  # model skipped the question
    assert r["ambiguous"] == ["Gupta"] and count(session, Customer) == 1
    session.commit()  # next message from the owner
    assert shop.add_credit(session, owner, "Gupt", 50, new_customer=True)["ok"]  # now we did ask
    assert count(session, Customer) == 2


def test_name_check_expires(session, owner):
    from datetime import timedelta

    shop.add_credit(session, owner, "Gupta", 100)
    shop.add_credit(session, owner, "Gupt", 50)
    log = session.scalar(select(ActionLog).where(ActionLog.tool_name == "name_check"))
    log.created_at = log.created_at - timedelta(minutes=11)
    session.flush()
    assert not shop.add_credit(session, owner, "Gupt", 50, new_customer=True)["ok"]


def test_new_customer_flag_needs_the_owners_answer_not_the_same_turn(session, owner):
    # The model asks and then immediately sets the flag itself, in one turn: the owner never answered.
    shop.add_credit(session, owner, "Gupta", 100)
    shop.add_credit(session, owner, "Gupt", 50)
    r = shop.add_credit(session, owner, "Gupt", 50, new_customer=True)
    assert not r["ok"] and count(session, Customer) == 1
