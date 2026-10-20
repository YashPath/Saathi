"""Shared DB fixtures. DB tests need TEST_DATABASE_URL (a throwaway database: tables are dropped)."""

import os

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db.models import Base

TEST_URL = os.getenv("TEST_DATABASE_URL")
needs_db = pytest.mark.skipif(not TEST_URL, reason="TEST_DATABASE_URL not set")


@pytest.fixture
def engine():
    eng = create_engine(TEST_URL)
    Base.metadata.drop_all(eng)
    Base.metadata.create_all(eng)
    yield eng
    Base.metadata.drop_all(eng)
    eng.dispose()


@pytest.fixture
def session(engine):
    with Session(engine) as s:
        yield s


from langchain_core.language_models.fake_chat_models import GenericFakeChatModel  # noqa: E402
from langchain_core.messages import AIMessage  # noqa: E402


class FakeLLM(GenericFakeChatModel):
    """Scripted chat model for offline agent tests; tools are 'bound' by ignoring them."""

    def bind_tools(self, tools, **kwargs):
        return self


def fake_llm(*steps):
    """Each step is either reply text or a list of (tool_name, args) calls."""
    msgs = []
    for i, step in enumerate(steps):
        if isinstance(step, str):
            msgs.append(AIMessage(step))
        else:
            calls = [{"name": n, "args": a, "id": f"call_{i}_{j}"} for j, (n, a) in enumerate(step)]
            msgs.append(AIMessage("", tool_calls=calls))
    return FakeLLM(messages=iter(msgs))


def accept_invites(session):
    """Test helper: every invited student accepts (as if each replied "join <class>"), and invites leave the outbox."""
    from datetime import datetime, timezone

    from sqlalchemy import update

    from app.agent import outbox
    from app.db.models import Student

    session.execute(update(Student).where(Student.user_id.is_not(None), Student.joined_at.is_(None))
                    .values(joined_at=datetime.now(timezone.utc)))
    session.flush()
    session.expire_all()
    outbox.take(session)
