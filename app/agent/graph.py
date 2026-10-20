"""Core agent: one entry point for every channel (CLI, app)."""

import logging

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langgraph.prebuilt import create_react_agent
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agent.approval import cancel_pending, resolve_pending, run_tool
from app.agent.llm import get_llm
from app.agent.outbox import take
from app.agent.roles.base import build_tools
from app.agent.roles.registry import get_role
from app.config import get_settings
from app.db.models import Message, User

log = logging.getLogger(__name__)

NOT_REGISTERED = "Sorry, this number is not registered with Saathi."
FAILED = "Sorry, something went wrong and nothing was saved. Please try again."

COMMON_PROMPT = """You are Saathi, a helpful assistant in the Saathi app.
- Reply in the user's language: English, Hindi written in Roman script, or Hinglish. Match their style.
- Keep replies short, like a chat message. Plain text only: no markdown, no ** or #.
- For approvals, tell the user to tap Haan or Nahi.
- One message may contain several tasks. Do all of them, calling tools one by one, then reply once.
- Always repeat the names and amounts that tools return. Never invent numbers; only use tool results.
"""


def load_history(session: Session, user_id: int) -> list[BaseMessage]:
    rows = session.scalars(
        select(Message).where(Message.user_id == user_id).order_by(Message.id.desc()).limit(get_settings().history_limit)
    ).all()
    return [HumanMessage(m.content) if m.role == "user" else AIMessage(m.content) for m in reversed(rows)]


def run_agent(session: Session, user: User, text: str, llm: BaseChatModel) -> str:
    """Run the role's agent. Only that role's tools are given to the model (role enforcement)."""
    role = get_role(user.role)
    tools = build_tools(role, session, user.id, run=lambda fn, args: run_tool(session, user.id, role, fn, args))
    agent = create_react_agent(llm, tools, prompt=COMMON_PROMPT + role.system_prompt)
    # max_concurrency=1: Gemini can return several tool calls at once and LangGraph would run them in
    # parallel threads, but they all share one DB session, which is not thread-safe.
    result = agent.invoke({"messages": [*load_history(session, user.id), HumanMessage(text)]},
                          {"max_concurrency": 1})
    return result["messages"][-1].text


def handle_message(session: Session, phone: str, text: str, llm: BaseChatModel | None = None) -> str:
    """Process one incoming message and return the reply. Caller commits."""
    user = session.scalar(select(User).where(User.phone == phone))
    if user is None:
        return NOT_REGISTERED
    user_id = user.id  # read now: after a failed flush the session cannot load it
    try:
        # a YES/NO to a pending approval is answered without calling the LLM
        reply = resolve_pending(session, user.id, get_role(user.role), text)
        if reply is None:
            # moving on cancels any pending approval, so a later "haan" can never confirm an old payment
            cancelled = cancel_pending(session, user.id)
            reply = run_agent(session, user, text, llm or get_llm())
            if cancelled:
                reply += f"\n\n(Not saved, approval cancelled: {cancelled})"
    except Exception:
        log.exception("agent failed for user %s", user_id)
        session.rollback()  # drop half-done writes so a retry cannot double-count
        take(session)  # and the messages those writes queued
        return FAILED
    session.add_all([Message(user_id=user.id, role="user", content=text),
                     Message(user_id=user.id, role="assistant", content=reply)])
    session.flush()
    return reply
