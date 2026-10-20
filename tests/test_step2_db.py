"""Step 2 checks: models, constraints, migration and seed. Needs TEST_DATABASE_URL."""

from decimal import Decimal

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, func, inspect, select
from sqlalchemy.exc import IntegrityError

from app.config import Settings
from app.db.models import Base, Customer, LedgerEntry, User
from seed import seed
from tests.conftest import TEST_URL, needs_db

pytestmark = needs_db


def test_phone_is_unique(session):
    session.add(User(phone="911111111111", name="A", role="shopkeeper"))
    session.commit()
    session.add(User(phone="911111111111", name="B", role="professor"))
    with pytest.raises(IntegrityError):
        session.commit()


def test_invalid_role_rejected(session):
    session.add(User(phone="912222222222", name="X", role="admin"))
    with pytest.raises(IntegrityError):
        session.commit()


def test_ledger_amount_is_exact_decimal(session):
    owner = User(phone="913333333333", name="Shop", role="shopkeeper")
    session.add(owner)
    session.flush()
    cust = Customer(owner_id=owner.id, name="Sharma")
    session.add(cust)
    session.flush()
    session.add(LedgerEntry(owner_id=owner.id, customer_id=cust.id, amount=Decimal("500")))
    session.commit()
    session.expire_all()
    amount = session.scalar(select(LedgerEntry.amount))
    assert amount == Decimal("500.00") and isinstance(amount, Decimal)


def test_seed_is_idempotent(session):
    settings = Settings(
        _env_file=None, seed_shopkeeper_phone="914444444444", seed_professor_phone="915555555555"
    )
    assert len(seed(session, settings)) == 2
    assert seed(session, settings) == []
    assert session.scalar(select(func.count()).select_from(User)) == 2


def test_migration_up_and_down():
    eng = create_engine(TEST_URL)
    Base.metadata.drop_all(eng)
    with eng.begin() as conn:
        conn.exec_driver_sql("DROP TABLE IF EXISTS alembic_version")
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", TEST_URL)
    command.upgrade(cfg, "head")
    tables = set(inspect(eng).get_table_names())
    assert {"users", "customers", "ledger_entries", "action_logs", "pending_actions", "messages"} <= tables
    command.downgrade(cfg, "base")
    assert inspect(eng).get_table_names() == ["alembic_version"]
    eng.dispose()


def test_downgrade_below_students_works_after_a_student_chatted():
    eng = create_engine(TEST_URL)
    Base.metadata.drop_all(eng)
    with eng.begin() as conn:
        conn.exec_driver_sql("DROP TABLE IF EXISTS alembic_version")
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", TEST_URL)
    command.upgrade(cfg, "head")
    with eng.begin() as conn:  # a student who used the bot has messages, logs and maybe a pending action
        conn.exec_driver_sql("INSERT INTO users (phone, name, role) VALUES ('919999999999', 'S', 'student')")
        conn.exec_driver_sql("INSERT INTO messages (user_id, role, content) SELECT id, 'user', 'hi' FROM users")
        conn.exec_driver_sql("INSERT INTO action_logs (user_id, tool_name, input_json, status) "
                             "SELECT id, 'my_attendance', '{}', 'done' FROM users")
    command.downgrade(cfg, "base")
    assert inspect(eng).get_table_names() == ["alembic_version"]
    eng.dispose()
