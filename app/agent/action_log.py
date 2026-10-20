"""Audit log: every successful tool call is recorded here."""

from typing import Any

from sqlalchemy.orm import Session

from app.db.models import ActionLog


def log_action(
    session: Session,
    user_id: int,
    tool_name: str,
    input: dict[str, Any],
    output: dict[str, Any] | None,
    status: str = "done",
) -> ActionLog:
    """Add one ActionLog row (flushed, not committed)."""
    row = ActionLog(user_id=user_id, tool_name=tool_name, input_json=input, output_json=output, status=status)
    session.add(row)
    session.flush()
    return row
