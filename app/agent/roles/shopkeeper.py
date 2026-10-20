"""Shopkeeper tools: a customer credit (udhaar) ledger.

Each tool takes (session, owner_id, ...) and returns {"ok": bool, "message": str, ...}.
User mistakes never raise. Tools flush; the caller commits (see app.db.session.get_session).
"""

import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from rapidfuzz import fuzz
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.agent.action_log import log_action
from app.db.models import ActionLog, Customer, LedgerEntry

MAX_AMOUNT = Decimal("1000000")
FUZZY_CUTOFF = 85  # "close enough to ask about"
SAME_NAME_CUTOFF = 90  # "close enough to use": typos like Sharmaa pass, Anil vs Sunil (85.7) does not
LEDGER_TOOLS = ("add_credit", "record_payment")
NAME_CHECK_TTL = timedelta(minutes=10)

# --- helpers -----------------------------------------------------------------


def normalise(name: str) -> str:
    """'Sharma ji', 'sharmaji', ' SHARMA ' -> 'sharma'."""
    n = re.sub(r"\s+", " ", name.strip().lower())
    # shortcut: also strips a glued suffix, so "Balaji" -> "bala"; fine for matching, revisit if names look odd
    return re.sub(r"\s*(ji ka|ji|bhai)$", "", n).strip() or n


def parse_amount(value: Any) -> Decimal | None:
    """Return a positive amount with at most 2 decimals and <= MAX_AMOUNT, else None."""
    # only at the start or end: stripping "rs" from the middle would glue "500rs.50" into 50050
    cur = r"(₹|rs\.?|rupees?|rupaye)"
    text = re.sub(rf"^\s*{cur}|{cur}\s*$", "", str(value).strip(), flags=re.IGNORECASE)
    try:
        amount = Decimal(text.replace(",", "").strip())
    except InvalidOperation:
        return None
    if not amount.is_finite() or amount <= 0 or amount > MAX_AMOUNT or amount.as_tuple().exponent < -2:
        return None
    return amount.quantize(Decimal("0.01"))


def rupees(amount: Decimal) -> str:
    return f"₹{amount:,.2f}"


def find_named(items: list, name: str, norm=normalise) -> tuple[Any, list[str]]:
    """Match a spoken name against rows with a .name. Returns (row, []), (None, [names]) to ask, or (None, [])."""
    target = norm(name)
    exact = [c for c in items if norm(c.name) == target]
    if exact:
        return exact[0], []
    close = [c for c in items if fuzz.WRatio(target, norm(c.name)) >= FUZZY_CUTOFF]
    if len(close) > 1:
        return None, sorted(c.name for c in close)
    if close:
        score = fuzz.ratio(target, norm(close[0].name))
        if score >= SAME_NAME_CUTOFF:  # a typo of the same name
            return close[0], []
        if score >= FUZZY_CUTOFF:  # nearly the same (Gupt/Gupta, Anil/Sunil): ask, never guess or split a balance
            return None, [close[0].name]
    return None, []  # "Ram" vs "Ramesh" lands here: clearly a different name


def find_customer(session: Session, owner_id: int, name: str) -> tuple[Customer | None, list[str]]:
    """Return (customer, []) on a match, (None, [names]) if ambiguous, (None, []) if not found."""
    return find_named(session.scalars(select(Customer).where(Customer.owner_id == owner_id)).all(), name)


def asked_recently(session: Session, owner_id: int, name: str) -> bool:
    """Did add_credit ask "Did you mean ...?" about this exact name in the last few minutes?"""
    since = datetime.now(timezone.utc) - NAME_CHECK_TTL
    rows = session.scalars(select(ActionLog.input_json).where(
        ActionLog.user_id == owner_id, ActionLog.tool_name == "name_check", ActionLog.created_at >= since,
        # now() is the transaction start: a question asked in this same turn does not count, the owner must answer first
        ActionLog.created_at < func.now()))
    return any(r.get("name") == normalise(name) for r in rows)


def balance_of(session: Session, customer_id: int) -> Decimal:
    total = session.scalar(select(func.sum(LedgerEntry.amount)).where(LedgerEntry.customer_id == customer_id))
    return total or Decimal("0.00")


def ambiguous(names: list[str]) -> dict[str, Any]:
    question = f"Did you mean {names[0]}?" if len(names) == 1 else f"Which customer do you mean: {', '.join(names)}?"
    return {"ok": False, "ambiguous": names, "message": f"Not saved. {question}"}


def not_found(name: str) -> dict[str, Any]:
    return {"ok": False, "message": f"No customer named '{name}' found."}


BAD_AMOUNT = {"ok": False, "message": "Please give a valid amount (more than 0, up to ₹10,00,000, max 2 decimals)."}


def _entry(session: Session, owner_id: int, tool: str, args: dict, customer: Customer, amount: Decimal, note: str | None):
    """Write one ledger entry plus its ActionLog and build the read-back reply."""
    entry = LedgerEntry(owner_id=owner_id, customer_id=customer.id, amount=amount, note=note)
    session.add(entry)
    session.flush()
    balance = balance_of(session, customer.id)
    verb = "Added" if amount > 0 else "Recorded"
    kind = "credit to" if amount > 0 else "payment from"
    result = {
        "ok": True,
        "customer": customer.name,
        "amount": str(abs(amount)),
        "balance": str(balance),
        "ledger_entry_id": entry.id,
        "message": f"{verb} {rupees(abs(amount))} {kind} {customer.name}. New balance: {rupees(balance)}",
    }
    log_action(session, owner_id, tool, args, result)
    return result


# --- tools -------------------------------------------------------------------


def add_credit(
    session: Session, owner_id: int, customer_name: str, amount: Any, note: str | None = None,
    new_customer: bool = False,
) -> dict:
    """Give credit (udhaar) to a customer; creates the customer if new.
    Set new_customer=true only after the owner confirms a similar-named customer is a different person."""
    value = parse_amount(amount)
    if value is None:
        return BAD_AMOUNT
    if not normalise(customer_name or ""):
        return {"ok": False, "message": "Please tell me the customer's name."}
    # The flag only counts after we asked "Did you mean X?" about this name, so the model cannot split a balance alone.
    new_customer = new_customer and asked_recently(session, owner_id, customer_name)
    if new_customer:  # exact name still reuses the existing customer (names are unique per shop)
        target = normalise(customer_name)
        customer = next((c for c in session.scalars(select(Customer).where(Customer.owner_id == owner_id))
                         if normalise(c.name) == target), None)
    else:
        customer, options = find_customer(session, owner_id, customer_name)
        if options:
            r = ambiguous(options)
            if len(options) == 1:  # only here: in record_payment this hint would turn a payment into a credit
                r["message"] += " If it is a different person, call add_credit again with new_customer=true."
                log_action(session, owner_id, "name_check", {"name": normalise(customer_name)}, {"asked": options[0]})
            return r
    if customer is None:
        customer = Customer(owner_id=owner_id, name=normalise(customer_name).title())
        session.add(customer)
        session.flush()
    args = {"customer_name": customer_name, "amount": str(value), "note": note}
    return _entry(session, owner_id, "add_credit", args, customer, value, note)


def record_payment(session: Session, owner_id: int, customer_name: str, amount: Any) -> dict:
    """Record money received from an existing customer."""
    value = parse_amount(amount)
    if value is None:
        return BAD_AMOUNT
    customer, options = find_customer(session, owner_id, customer_name)
    if options:
        return ambiguous(options)
    if customer is None:
        return not_found(customer_name)
    args = {"customer_name": customer_name, "amount": str(value)}
    return _entry(session, owner_id, "record_payment", args, customer, -value, None)


def get_balance(session: Session, owner_id: int, customer_name: str) -> dict:
    """How much a customer currently owes."""
    customer, options = find_customer(session, owner_id, customer_name)
    if options:
        return ambiguous(options)
    if customer is None:
        return not_found(customer_name)
    balance = balance_of(session, customer.id)
    result = {"ok": True, "customer": customer.name, "balance": str(balance),
              "message": f"{customer.name} owes {rupees(balance)}."}
    log_action(session, owner_id, "get_balance", {"customer_name": customer_name}, result)
    return result


def list_dues(session: Session, owner_id: int) -> dict:
    """All customers with a positive balance, highest first."""
    total = func.sum(LedgerEntry.amount)
    rows = session.execute(
        select(Customer.name, total)
        .join(LedgerEntry, LedgerEntry.customer_id == Customer.id)
        .where(Customer.owner_id == owner_id)
        .group_by(Customer.id, Customer.name)
        .having(total > 0)
        .order_by(total.desc())
    ).all()
    dues = [{"customer": name, "balance": str(bal)} for name, bal in rows]
    message = "\n".join(f"{name}: {rupees(bal)}" for name, bal in rows) or "No pending dues."
    result = {"ok": True, "dues": dues, "message": message}
    log_action(session, owner_id, "list_dues", {}, result)
    return result


def undo_last_entry(session: Session, owner_id: int) -> dict:
    """Reverse this owner's latest credit/payment with an opposite entry (rows are never deleted)."""
    log = session.scalars(
        select(ActionLog)
        .where(ActionLog.user_id == owner_id, ActionLog.tool_name.in_((*LEDGER_TOOLS, "create_bill", "cancel_bill")),
               ActionLog.status == "done")
        .order_by(ActionLog.id.desc())
        .limit(1)
        .with_for_update()  # two "undo" at once must not reverse the same entry twice
    ).first()
    if log is None:
        return {"ok": False, "message": "There is nothing to undo."}
    if log.tool_name == "cancel_bill":  # reaching past it would silently reverse an older, unrelated entry
        return {"ok": False, "message": f"Not done: the last entry is the cancellation of Bill #{log.output_json['bill_no']}, "
                "which cannot be undone. Make a new bill instead if it was a mistake."}
    if log.tool_name == "create_bill":
        # undoing only the credit would leave the bill and the sold stock behind: bills go through cancel_bill
        return {"ok": False, "message": f"Not done: the last entry is Bill #{log.output_json['bill_no']}. "
                f"To reverse a bill, use cancel_bill with bill number {log.output_json['bill_no']}."}
    original = session.get(LedgerEntry, log.output_json["ledger_entry_id"])
    reverse = LedgerEntry(owner_id=owner_id, customer_id=original.customer_id, amount=-original.amount,
                          note=f"Undo of entry #{original.id}")
    session.add(reverse)
    log.status = "undone"
    session.flush()
    customer = session.get(Customer, original.customer_id)
    balance = balance_of(session, customer.id)
    result = {"ok": True, "undone_entry_id": original.id, "customer": customer.name, "balance": str(balance),
              "message": f"Undid {rupees(abs(original.amount))} for {customer.name}. New balance: {rupees(balance)}"}
    log_action(session, owner_id, "undo_last_entry", {}, result)
    return result


# --- role pack ---------------------------------------------------------------

SYSTEM_PROMPT = """Role: you help a kirana shop owner keep a customer credit (udhaar) ledger.
- "udhaar likh do / add credit" -> add_credit. "paise de diye / paid / jama" -> record_payment.
- "kitna baaki / balance" -> get_balance. "sabka hisaab / who owes" -> list_dues. "undo / galti ho gayi" -> undo_last_entry.
- If a tool says a name is ambiguous or not found, ask the owner which customer they mean. Do not guess.
- If a tool asks "Did you mean X?" and the owner says it is a different person, call add_credit again with new_customer=true.
- If a tool result starts with "Not saved" or "Needs approval", never tell the owner it is done.
"""

TOOLS = [add_credit, record_payment, get_balance, list_dues, undo_last_entry]
