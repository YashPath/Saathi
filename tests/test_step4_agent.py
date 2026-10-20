"""Step 4 checks: core agent with a scripted (offline) LLM."""

import pytest
from langchain_core.messages import ToolMessage
from sqlalchemy import func, select

from app.agent import graph
from app.agent.roles.base import args_model
from app.agent.roles.registry import get_role
from app.agent.roles.shopkeeper import add_credit
from app.db.models import LedgerEntry, Message, User
from tests.conftest import fake_llm, needs_db

pytestmark = needs_db


@pytest.fixture
def users(session):
    session.add_all([User(phone="911", name="Shop", role="shopkeeper"), User(phone="922", name="Prof", role="professor")])
    session.flush()


def test_llm_never_sees_session_or_owner():
    fields = set(args_model(add_credit).model_fields)
    assert fields == {"customer_name", "amount", "note", "new_customer"}


def test_two_tasks_in_one_message(session, users):
    from app.agent.roles import shopkeeper as shop
    gupta_owner = session.scalar(select(User.id).where(User.phone == "911"))
    shop.add_credit(session, gupta_owner, "Gupta", 250)
    llm = fake_llm(
        [("add_credit", {"customer_name": "Sharma ji", "amount": "500"})],
        [("get_balance", {"customer_name": "Gupta"})],
        "Sharma ji ka ₹500 likh diya. Gupta ka balance ₹250 hai.",
    )
    reply = graph.handle_message(session, "911", "Sharma ji ka 500 udhaar likh do aur Gupta ka balance batao", llm)
    assert reply == "Sharma ji ka ₹500 likh diya. Gupta ka balance ₹250 hai."
    assert session.scalar(select(func.count()).select_from(LedgerEntry)) == 2


def test_tool_results_reach_the_model(session, users, monkeypatch):
    seen = []
    real = graph.create_react_agent

    def spy(*a, **k):
        agent = real(*a, **k)
        orig = agent.invoke
        agent.invoke = lambda inp, *a, **kw: seen.append(out := orig(inp, *a, **kw)) or out
        return agent

    monkeypatch.setattr(graph, "create_react_agent", spy)
    llm = fake_llm([("add_credit", {"customer_name": "Verma", "amount": "120"})], "done")
    graph.handle_message(session, "911", "Verma 120", llm)
    tool_msgs = [m for m in seen[0]["messages"] if isinstance(m, ToolMessage)]
    assert "Added ₹120.00 credit to Verma" in tool_msgs[0].content


def test_professor_cannot_use_ledger_tools(session, users):
    names = {t.__name__ for t in get_role("professor").tools}
    assert "mark_attendance" in names and not names & {"add_credit", "record_payment", "create_bill"}
    llm = fake_llm([("add_credit", {"customer_name": "X", "amount": "100"})], "I can't do that.")
    graph.handle_message(session, "922", "add 100 to X", llm)
    assert session.scalar(select(func.count()).select_from(LedgerEntry)) == 0


def test_professor_reaches_professor_tools(session, users):
    llm = fake_llm([("add_class", {"class_name": "CSE-3"})], "Class ban gayi.")
    assert graph.handle_message(session, "922", "CSE-3 class banao", llm) == "Class ban gayi."


def test_history_saved_and_reloaded(session, users):
    graph.handle_message(session, "911", "hello", fake_llm("Namaste!"))
    user_id = session.scalar(select(User.id).where(User.phone == "911"))
    history = graph.load_history(session, user_id)
    assert [m.content for m in history] == ["hello", "Namaste!"]
    assert session.scalar(select(func.count()).select_from(Message)) == 2


def test_unknown_number(session, users):
    assert graph.handle_message(session, "000", "hi", fake_llm("x")) == graph.NOT_REGISTERED


def test_llm_failure_saves_nothing(session, users):
    llm = fake_llm([("add_credit", {"customer_name": "Gupta", "amount": "100"})])  # runs out -> raises
    assert graph.handle_message(session, "911", "Gupta 100", llm) == graph.FAILED
    assert session.scalar(select(func.count()).select_from(LedgerEntry)) == 0


def test_quota_exhausted_says_so(session, users):
    llm = fake_llm()
    llm._generate = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("429 RESOURCE_EXHAUSTED. Quota exceeded"))
    assert graph.handle_message(session, "911", "Gupta 100", llm) == graph.LIMIT


def test_parallel_tool_calls_in_one_turn(session, users):
    # Gemini often returns several function calls in ONE message; LangGraph runs them in threads.
    for i in range(5):
        llm = fake_llm(
            [("add_credit", {"customer_name": "Sharma ji", "amount": "500"}), ("get_balance", {"customer_name": "Sharma"}),
             ("add_credit", {"customer_name": "Verma", "amount": "100"}), ("list_dues", {})],
            "done",
        )
        assert graph.handle_message(session, "911", "Sharma 500, Verma 100, sabka hisaab", llm) == "done"
    assert session.scalar(select(func.count()).select_from(LedgerEntry)) == 10


def test_amount_sent_as_number_is_accepted():
    assert args_model(add_credit)(customer_name="A", amount=500).amount == "500"


def test_failed_flush_inside_a_tool_returns_failed_not_a_crash(session, users, monkeypatch):
    from app.agent.action_log import log_action
    from app.agent.roles.base import Role

    def broken(session, owner_id):
        """A tool whose write fails at flush time, like any unexpected DB error."""
        log_action(session, owner_id, "broken", {"x": object()}, None)  # not JSON: the flush fails

    monkeypatch.setattr(graph, "get_role", lambda name: Role("shopkeeper", "", [broken]))
    assert graph.handle_message(session, "911", "hi", fake_llm([("broken", {})], "x")) == graph.FAILED
