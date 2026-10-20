"""Shopkeeper messages to other people: supplier orders and payment reminders, plus shop details.

Anything that messages a third party, or changes where customers pay, needs the owner's YES (approval gate).
Each such tool has a check_* validator shared by its approval rule and the tool itself, so the rule and the tool
can never disagree: an invalid request is answered directly, a valid one always waits for YES.
"""

import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from urllib.parse import quote, urlencode

from pydantic import BaseModel, ConfigDict, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agent.action_log import log_action
from app.agent.outbox import queue_file, queue_text
from app.agent.roles import shopkeeper as ledger
from app.agent.roles.inventory import find_product, parse_qty, qty_text
from app.config import get_settings
from app.db.models import ActionLog, Customer, Supplier, User

UPI_RE = re.compile(r"^[a-zA-Z0-9._-]{2,256}@[a-zA-Z][a-zA-Z0-9]{1,63}$")
GSTIN_RE = re.compile(r"^\d{2}[A-Z]{5}\d{4}[A-Z][1-9A-Z]Z[0-9A-Z]$")
REMINDER_GAP = timedelta(hours=24)  # never nag the same customer twice a day
MAX_ORDER_LINES = 50


class OrderLine(BaseModel):
    model_config = ConfigDict(coerce_numbers_to_str=True)
    product_name: str
    qty: str
    unit: str | None = None


def parse_phone(value: str | None) -> str | None:
    """Indian 10-digit numbers get 91; otherwise 11-15 digits with country code. Returns digits only."""
    digits = re.sub(r"\D", "", value or "")
    if len(digits) == 11 and digits[0] == "0":  # 09876543210: Indian trunk prefix
        digits = digits[1:]
    if digits.startswith("0"):
        return None
    if len(digits) == 10 and digits[0] in "6789":
        return "91" + digits
    if len(digits) == 12 and digits.startswith("91") and digits[2] in "6789":
        return digits
    if 11 <= len(digits) <= 15 and not digits.startswith("91"):
        return digits
    return None


def find_supplier(session: Session, owner_id: int, name: str) -> tuple[Supplier | None, list[str]]:
    rows = session.scalars(select(Supplier).where(Supplier.owner_id == owner_id)).all()
    return ledger.find_named(rows, name)


# --- shop and contact details -------------------------------------------------


def check_shop_details(session: Session, owner_id: int, upi_id: str | None = None, gstin: str | None = None):
    if not upi_id and not gstin:
        return {"ok": False, "message": "Not saved: tell me the UPI ID and/or GSTIN to set."}, {}
    clean = {}
    if upi_id:
        if not UPI_RE.match(upi_id.strip()):
            return {"ok": False, "message": f"Not saved: '{upi_id}' does not look like a UPI ID (name@bank)."}, {}
        clean["upi_id"] = upi_id.strip()
    if gstin:
        if not GSTIN_RE.match(gstin.strip().upper()):
            return {"ok": False, "message": f"Not saved: '{gstin}' is not a valid 15-character GSTIN."}, {}
        clean["gstin"] = gstin.strip().upper()
    return None, clean


def set_shop_details(session: Session, owner_id: int, upi_id: str | None = None, gstin: str | None = None) -> dict:
    """Set the shop's UPI ID (for payment reminders) and/or GSTIN (printed on bills). A new UPI ID needs approval."""
    error, clean = check_shop_details(session, owner_id, upi_id, gstin)
    if error:
        return error
    owner = session.get(User, owner_id)
    for key, value in clean.items():
        setattr(owner, key, value)
    session.flush()
    result = {"ok": True, "message": "Saved: " + ", ".join(f"{k.upper().replace('_', ' ')} {v}" for k, v in clean.items()) + "."}
    log_action(session, owner_id, "set_shop_details", clean, result)
    return result


def set_customer_phone(session: Session, owner_id: int, customer_name: str, phone: str) -> dict:
    """Save a customer's WhatsApp number (needed for payment reminders)."""
    number = parse_phone(phone)
    if number is None:
        return {"ok": False, "message": f"Not saved: '{phone}' is not a valid mobile number."}
    customer, options = ledger.find_customer(session, owner_id, customer_name)
    if options:
        return ledger.ambiguous(options)
    if customer is None:
        return ledger.not_found(customer_name)
    customer.phone = number
    session.flush()
    result = {"ok": True, "message": f"Saved {customer.name}'s number: +{number}."}
    log_action(session, owner_id, "set_customer_phone", {"customer_name": customer_name, "phone": number}, result)
    return result


def add_supplier(session: Session, owner_id: int, supplier_name: str, phone: str) -> dict:
    """Add a supplier (wholesaler) with their WhatsApp number, or update the number of an existing one."""
    number = parse_phone(phone)
    if number is None:
        return {"ok": False, "message": f"Not saved: '{phone}' is not a valid mobile number."}
    if not ledger.normalise(supplier_name or ""):
        return {"ok": False, "message": "Not saved: what is the supplier's name?"}
    supplier, options = find_supplier(session, owner_id, supplier_name)
    if options:
        return {"ok": False, "ambiguous": options, "message": f"Not saved. Which supplier: {', '.join(options)}?"}
    verb = "Updated" if supplier else "Added"
    if supplier is None:
        supplier = Supplier(owner_id=owner_id, name=supplier_name.strip().title(), phone=number)
        session.add(supplier)
    else:
        supplier.phone = number
    session.flush()
    result = {"ok": True, "message": f"{verb} supplier {supplier.name}: +{number}."}
    log_action(session, owner_id, "add_supplier", {"supplier_name": supplier_name, "phone": number}, result)
    return result


# --- supplier orders ----------------------------------------------------------


def check_order(session: Session, owner_id: int, supplier_name: str, items: list, note: str | None = None):
    """Return (error, None) or (None, (supplier, [(name, qty, unit)]))."""
    supplier, options = find_supplier(session, owner_id, supplier_name or "")
    if options:
        return {"ok": False, "ambiguous": options, "message": f"Not sent. Which supplier: {', '.join(options)}?"}, None
    if supplier is None:
        return {"ok": False, "message": f"Not sent: no supplier named '{supplier_name}'. Add them with their number first."}, None
    try:
        lines = [OrderLine.model_validate(i) for i in items or []]
    except ValidationError:
        return {"ok": False, "message": "Not sent: each item needs a product name and a quantity."}, None
    if not lines or len(lines) > MAX_ORDER_LINES:
        return {"ok": False, "message": f"Not sent: an order needs 1 to {MAX_ORDER_LINES} items."}, None
    parsed = []
    for line in lines:
        qty = parse_qty(line.qty)
        if qty is None or not line.product_name.strip():
            return {"ok": False, "message": f"Not sent: '{line.qty}' is not a valid quantity for {line.product_name}."}, None
        product, _ = find_product(session, owner_id, line.product_name)  # new items may be ordered too
        parsed.append((product.name if product else line.product_name.strip(), qty,
                       line.unit or (product.unit if product else "")))
    return None, (supplier, parsed)


def order_from_supplier(session: Session, owner_id: int, supplier_name: str, items: list[OrderLine],
                        note: str | None = None) -> dict:
    """Prepare a stock order for a supplier (the owner sends it from the app via WhatsApp/SMS). items is a list of {product_name, qty, unit?}.
    Always waits for the owner's YES before it is sent."""
    error, order = check_order(session, owner_id, supplier_name, items, note)
    if error:
        return error
    supplier, parsed = order
    owner = session.get(User, owner_id)
    body = "\n".join(f"- {name}: {qty_text(qty)} {unit}".rstrip() for name, qty, unit in parsed)
    text = f"Order from {owner.name}:\n{body}" + (f"\nNote: {note.strip()}" if note and note.strip() else "") + \
        "\nPlease confirm. Thank you."
    queue_text(session, supplier.phone, text)
    result = {"ok": True, "message": f"Order sent to {supplier.name} (+{supplier.phone}):\n{body}"}
    log_action(session, owner_id, "order_from_supplier", {"supplier": supplier.name, "items": [
        {"product_name": n, "qty": str(q), "unit": u} for n, q, u in parsed], "note": note}, result)
    return result


# --- payment reminders --------------------------------------------------------


def upi_link(upi_id: str, payee: str, amount: Decimal, note: str) -> str:
    query = urlencode({"pa": upi_id, "pn": payee, "am": f"{amount:.2f}", "cu": "INR", "tn": note}, quote_via=quote)
    return f"upi://pay?{query}"


def check_reminder(session: Session, owner_id: int, customer_name: str):
    """Return (error, None) or (None, (customer, balance))."""
    customer, options = ledger.find_customer(session, owner_id, customer_name or "")
    if options:
        return ledger.ambiguous(options), None
    if customer is None:
        return ledger.not_found(customer_name), None
    if not customer.phone:
        return {"ok": False, "message": f"Not sent: I don't have {customer.name}'s number. Tell me and I will save it."}, None
    balance = ledger.balance_of(session, customer.id)
    if balance <= 0:
        return {"ok": False, "message": f"Not sent: {customer.name} has nothing pending."}, None
    since = datetime.now(timezone.utc) - REMINDER_GAP
    recent = session.scalars(select(ActionLog.input_json).where(
        ActionLog.user_id == owner_id, ActionLog.tool_name == "send_payment_reminder", ActionLog.status == "done",
        ActionLog.created_at >= since))
    if any(r.get("customer_id") == customer.id for r in recent):
        return {"ok": False, "message": f"Not sent: {customer.name} was already reminded in the last 24 hours."}, None
    return None, (customer, balance)


def send_payment_reminder(session: Session, owner_id: int, customer_name: str) -> dict:
    """Prepare a polite reminder of pending dues to a customer, with a UPI payment link and QR code
    when the shop's UPI ID is set. Always waits for the owner's YES before it is sent."""
    error, found = check_reminder(session, owner_id, customer_name)
    if error:
        return error
    customer, balance = found
    owner = session.get(User, owner_id)
    text = (f"Namaste {customer.name} ji, {owner.name} se yaad dilana tha: aapka {ledger.rupees(balance)} baaki hai. "
            "Jab ho sake, bhugtaan kar dijiye. Dhanyavaad!")
    if owner.upi_id:
        link = upi_link(owner.upi_id, owner.name, balance, f"Dues {owner.name}")
        text += f"\nUPI: {owner.upi_id}\nPay: {link}"
        qr = qr_code(owner_id, customer.id, link)
        queue_text(session, customer.phone, text)
        if qr:
            queue_file(session, customer.phone, str(qr), qr.name, kind="image")
    else:
        queue_text(session, customer.phone, text)
    note = "" if owner.upi_id else " Tip: set your UPI ID so reminders include a payment link."
    result = {"ok": True, "message": f"Reminder sent to {customer.name} for {ledger.rupees(balance)}.{note}"}
    log_action(session, owner_id, "send_payment_reminder", {"customer_id": customer.id, "amount": str(balance)}, result)
    return result


def qr_code(owner_id: int, customer_id: int, link: str) -> Path | None:
    import logging

    import qrcode

    try:
        folder = Path(get_settings().bills_dir) / str(owner_id)
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"upi_{customer_id}.png"
        qrcode.make(link).save(path)
        return path
    except Exception:
        logging.getLogger(__name__).exception("QR for customer %s failed", customer_id)
        return None


# --- approval rules (same validators as the tools) ----------------------------


def needs_approval_order(session: Session, owner_id: int, args: dict) -> bool:
    return check_order(session, owner_id, args.get("supplier_name"), args.get("items"), args.get("note"))[0] is None


def needs_approval_reminder(session: Session, owner_id: int, args: dict) -> bool:
    return check_reminder(session, owner_id, args.get("customer_name"))[0] is None


def needs_approval_shop_details(session: Session, owner_id: int, args: dict) -> bool:
    error, clean = check_shop_details(session, owner_id, args.get("upi_id"), args.get("gstin"))
    # where customers pay: never changed without YES (re-saving the same ID is not a change)
    return error is None and "upi_id" in clean and session.get(User, owner_id).upi_id != clean["upi_id"]


SYSTEM_PROMPT = """- Suppliers: "supplier add karo" -> add_supplier. "maal mangwao / order bhejo" -> order_from_supplier
  (check_stock with no name shows what is low). Orders wait for the owner's YES.
- Reminders: "Sharma ko yaad dilao" -> send_payment_reminder (waits for YES). Save numbers with set_customer_phone,
  and the shop's UPI ID / GSTIN with set_shop_details.
"""

TOOLS = [add_supplier, order_from_supplier, set_customer_phone, send_payment_reminder, set_shop_details]
APPROVAL_RULES = {
    "order_from_supplier": needs_approval_order,
    "send_payment_reminder": needs_approval_reminder,
    "set_shop_details": needs_approval_shop_details,
}
