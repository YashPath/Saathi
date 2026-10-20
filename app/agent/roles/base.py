"""Role packs: a system prompt plus the tools a user of that role may use."""

import inspect
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from langchain_core.tools import StructuredTool
from pydantic import ConfigDict, create_model
from sqlalchemy.orm import Session

ToolFn = Callable[..., dict]


@dataclass
class Role:
    name: str
    system_prompt: str
    tools: list[ToolFn]
    # tool name -> rule(session, user_id, args); True means the call must wait for the user's YES
    approval_rules: dict[str, Callable[[Session, int, dict], bool]] = field(default_factory=dict)

    def tool(self, name: str) -> ToolFn | None:
        return next((t for t in self.tools if t.__name__ == name), None)


def args_model(fn: ToolFn):
    """Pydantic schema of fn's parameters, skipping (session, owner_id) which the LLM must never set."""
    params = list(inspect.signature(fn).parameters.values())[2:]
    fields = {
        # untyped params (e.g. amount) become str: LLM schemas need a concrete type and the tools parse strings
        p.name: (str if p.annotation in (inspect.Parameter.empty, Any) else p.annotation,
                 ... if p.default is inspect.Parameter.empty else p.default)
        for p in params
    }
    # Gemini may send amount as the number 500; accept it instead of costing a retry round trip
    return create_model(f"{fn.__name__}_args", __config__=ConfigDict(coerce_numbers_to_str=True), **fields)


def build_tools(role: Role, session: Session, user_id: int, run: Callable[..., dict]) -> list[StructuredTool]:
    """LangChain tools for this role only. `run(fn, args)` executes a call (approval hook lives there)."""
    tools = []
    for fn in role.tools:
        schema = args_model(fn)

        def call(_fn=fn, _schema=schema, **kwargs: Any) -> dict:
            # plain JSON values: args are stored (pending approvals, logs) and nested items arrive as models
            return run(_fn, _schema.model_validate(kwargs).model_dump(mode="json", exclude_unset=True))

        tools.append(StructuredTool.from_function(
            func=call, name=fn.__name__, description=inspect.getdoc(fn) or fn.__name__, args_schema=schema
        ))
    return tools
