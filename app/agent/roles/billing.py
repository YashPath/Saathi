"""Shopkeeper billing: one spoken order becomes a GST bill, stock goes down, a PDF goes to the owner."""

import logging
from xml.sax.saxutils import escape
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.agent.action_log import log_action
from app.agent.outbox import queue_file
from app.agent.roles import shopkeeper as ledger
from app.agent.roles.inventory import ask_product, find_product, parse_qty, qty_text
from app.config import get_settings
from app.db.models import ActionLog, Bill, BillItem, Customer, LedgerEntry, Product, User

log = logging.getLogger(__name__)
IST = ZoneInfo("Asia/Kolkata")
MAX_LINES = 50
PAISA = Decimal("0.01")


class BillLine(BaseModel):
    model_config = ConfigDict(coerce_numbers_to_str=True)
    product_name: str
    qty: str


def gst_split(amount: Decimal, rate: Decimal) -> tuple[Decimal, Decimal]:
    """MRP includes GST: return (taxable value, GST) for one line, rounded to paise."""
    taxable = (amount * 100 / (100 + rate)).quantize(PAISA, ROUND_HALF_UP)
    return taxable, amount - taxable


def create_bill(
    session: Session, owner_id: int, items: list[BillLine], customer_name: str | None = None, on_credit: bool = False,
) -> dict:
    """Make a bill for a sale: items is a list of {product_name, qty}. Reduces stock and sends the PDF to the owner.
    on_credit=true adds the bill total to the customer's credit (udhaar); it needs customer_name."""
    try:
        lines = [BillLine.model_validate(i) for i in items or []]
    except ValidationError:
        return {"ok": False, "message": "Not saved: each item needs a product name and a quantity."}
    if not lines or len(lines) > MAX_LINES:
        return {"ok": False, "message": f"Not saved: a bill needs 1 to {MAX_LINES} items."}
    if on_credit and not (customer_name or "").strip():
        return {"ok": False, "message": "Not saved: whose credit (udhaar) should this bill go to?"}

    # Serialise bills per shop: bill numbers and stock checks must not interleave.
    owner = session.get(User, owner_id, with_for_update=True)

    # 1. Validate everything before writing anything (all-or-nothing).
    wanted: dict[int, list] = {}  # product id -> [product, total qty]; repeated lines are merged
    for line in lines:
        qty = parse_qty(line.qty)
        if qty is None:
            return {"ok": False, "message": f"Not saved: '{line.qty}' is not a valid quantity for {line.product_name}."}
        product, options = find_product(session, owner_id, line.product_name)
        if options:
            return ask_product(options)
        if product is None:
            return {"ok": False, "message": f"Not saved: no product named '{line.product_name}'. Add it first."}
        wanted.setdefault(product.id, [product, Decimal("0")])[1] += qty
    for product, qty in wanted.values():
        session.refresh(product, with_for_update=True)  # read the latest stock under a row lock
        if qty > product.stock:
            return {"ok": False, "message": f"Not saved: only {qty_text(product.stock)} {product.unit} of "
                                            f"{product.name} in stock, bill asks for {qty_text(qty)}."}
    priced = []
    for product, qty in wanted.values():
        amount = (product.price * qty).quantize(PAISA, ROUND_HALF_UP)
        priced.append((product, qty, amount, *gst_split(amount, product.gst_rate)))
    total = sum((p[2] for p in priced), Decimal("0"))
    if total > ledger.MAX_AMOUNT:
        return {"ok": False, "message": "Not saved: bill total is above ₹10,00,000."}
    customer = None
    if customer_name:
        customer, options = ledger.find_customer(session, owner_id, customer_name)
        if options:
            return ledger.ambiguous(options)
        if customer is None and not on_credit:
            return ledger.not_found(customer_name)

    # 2. Write.
    bill_no = (session.scalar(select(func.max(Bill.bill_no)).where(Bill.owner_id == owner_id)) or 0) + 1
    gst_total = sum((p[4] for p in priced), Decimal("0"))
    bill = Bill(owner_id=owner_id, bill_no=bill_no, customer_id=customer.id if customer else None,
                on_credit=on_credit, taxable=total - gst_total, gst_total=gst_total, total=total)
    session.add(bill)
    session.flush()
    for product, qty, amount, _taxable, gst in priced:
        product.stock -= qty
        session.add(BillItem(bill_id=bill.id, product_id=product.id, name=product.name, qty=qty, unit=product.unit,
                             price=product.price, gst_rate=product.gst_rate, amount=amount, gst_amount=gst))
    session.flush()

    lines_text = "\n".join(f"{p.name} {qty_text(q)} {p.unit} × {ledger.rupees(p.price)} = {ledger.rupees(a)}"
                           for p, q, a, *_ in priced)
    message = f"Bill #{bill_no}\n{lines_text}\nTotal {ledger.rupees(total)} (incl. GST {ledger.rupees(gst_total)})."
    if on_credit:
        credit = ledger.add_credit(session, owner_id, customer_name, str(total), note=f"Bill #{bill_no}")
        if not credit["ok"]:  # cannot happen after the checks above; never keep a credit bill without its credit
            raise RuntimeError(f"credit for bill #{bill_no} failed: {credit['message']}")
        bill.customer_id = ledger.find_customer(session, owner_id, customer_name)[0].id
        message += f"\nAdded to {credit['customer']}'s credit. New balance: {ledger.rupees(Decimal(credit['balance']))}."
        credit_entry_id = credit["ledger_entry_id"]

    pdf = write_pdf(owner, bill, customer_name=credit["customer"] if on_credit else (customer.name if customer else None))
    if pdf:
        queue_file(session, owner.phone, str(pdf), pdf.name)
        message += "\nPDF bill sent."
    else:
        message += "\n(The PDF could not be made, but the bill is saved.)"
    result = {"ok": True, "bill_no": bill_no, "total": str(total), "gst_total": str(gst_total), "message": message,
              "credit_entry_id": credit_entry_id if on_credit else None}
    log_action(session, owner_id, "create_bill", {"items": [l.model_dump() for l in lines],
                                                  "customer_name": customer_name, "on_credit": on_credit}, result)
    return result


# --- cancelling ---------------------------------------------------------------


def check_cancel_bill(session: Session, owner_id: int, bill_no) -> tuple[dict | None, Bill | None]:
    try:
        number = int(str(bill_no).strip().lstrip("#"))
    except ValueError:
        return {"ok": False, "message": f"Not done: '{bill_no}' is not a bill number."}, None
    bill = session.scalar(select(Bill).where(Bill.owner_id == owner_id, Bill.bill_no == number).with_for_update())
    if bill is None:
        return {"ok": False, "message": f"Not done: there is no Bill #{number}."}, None
    if bill.cancelled_at is not None:
        return {"ok": False, "message": f"Not done: Bill #{number} is already cancelled."}, None
    return None, bill


def cancel_bill(session: Session, owner_id: int, bill_no: str) -> dict:
    """Cancel a bill by its number (a wrong bill or returned goods): the stock goes back and, for a credit bill,
    the customer's credit is reversed. The bill is kept, marked cancelled. Waits for the owner's YES."""
    error, bill = check_cancel_bill(session, owner_id, bill_no)
    if error:
        return error
    session.get(User, owner_id, with_for_update=True)  # same lock order as create_bill
    for item in bill_items(bill):
        product = session.get(Product, item.product_id, with_for_update=True)
        product.stock += item.qty
    message = f"Cancelled Bill #{bill.bill_no} ({ledger.rupees(bill.total)}). Stock returned."
    log = session.scalars(select(ActionLog).where(
        ActionLog.user_id == owner_id, ActionLog.tool_name == "create_bill",
        ActionLog.output_json["bill_no"].as_integer() == bill.bill_no)).first()
    if log:
        log.status = "undone"  # "undo" skips it
    if bill.on_credit:
        entry_id = (log.output_json or {}).get("credit_entry_id") if log else None
        entry = session.get(LedgerEntry, entry_id) if entry_id else session.scalar(select(LedgerEntry).where(
            LedgerEntry.owner_id == owner_id, LedgerEntry.note == f"Bill #{bill.bill_no}"))
        if entry is None:  # never leave a half-cancelled credit bill
            raise RuntimeError(f"credit entry for bill #{bill.bill_no} not found")
        session.add(LedgerEntry(owner_id=owner_id, customer_id=entry.customer_id, amount=-entry.amount,
                                note=f"Cancel of Bill #{bill.bill_no}"))
        for credit_log in session.scalars(select(ActionLog).where(
                ActionLog.user_id == owner_id, ActionLog.tool_name == "add_credit", ActionLog.status == "done")):
            if (credit_log.output_json or {}).get("ledger_entry_id") == entry.id:
                credit_log.status = "undone"  # so "undo" and the daily summary skip it
        session.flush()
        customer = session.get(Customer, entry.customer_id)
        balance = ledger.balance_of(session, customer.id)
        message += (f" {customer.name}'s credit reduced by {ledger.rupees(entry.amount)}; new balance "
                    f"{ledger.rupees(balance)}.")
        if balance < 0:  # they had already paid for goods now returned
            message += f" {customer.name} has paid more than they owe: you owe them {ledger.rupees(-balance)} back."
    bill.cancelled_at = datetime.now(IST)
    session.flush()
    result = {"ok": True, "bill_no": bill.bill_no, "message": message}
    log_action(session, owner_id, "cancel_bill", {"bill_no": bill.bill_no}, result)
    return result


def needs_approval_cancel_bill(session: Session, owner_id: int, args: dict) -> bool:
    return check_cancel_bill(session, owner_id, args.get("bill_no"))[0] is None


def write_pdf(owner: User, bill: Bill, customer_name: str | None) -> Path | None:
    """Render the bill as a PDF. Returns None (and logs) on failure; the bill is saved either way."""
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A5
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    def rs(x: Decimal) -> str:  # the built-in PDF fonts have no ₹ glyph
        return f"Rs. {x:,.2f}"

    try:
        folder = Path(get_settings().bills_dir) / str(owner.id)
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"bill_{bill.bill_no}.pdf"
        styles = getSampleStyleSheet()
        title = "Tax Invoice" if owner.gstin else "Bill"
        when = (bill.created_at or datetime.now(IST)).astimezone(IST).strftime("%d %b %Y, %I:%M %p")
        head = [Paragraph(f"<b>{escape(owner.name)}</b>", styles["Title"]), Paragraph(title, styles["Heading3"])]
        if owner.gstin:
            head.append(Paragraph(f"GSTIN: {owner.gstin}", styles["Normal"]))
        head.append(Paragraph(f"Bill #{bill.bill_no} &nbsp; {when}", styles["Normal"]))
        if customer_name:
            head.append(Paragraph(f"Customer: {escape(customer_name)}" + (" (on credit)" if bill.on_credit else ""), styles["Normal"]))
        rows = [["Item", "Qty", "Rate", "GST", "Amount"]]
        rows += [[i.name, f"{qty_text(i.qty)} {i.unit}", rs(i.price), f"{qty_text(i.gst_rate)}%", rs(i.amount)]
                 for i in bill_items(bill)]
        half = (bill.gst_total / 2).quantize(PAISA, ROUND_HALF_UP)
        rows += [["", "", "", "Taxable", rs(bill.taxable)], ["", "", "", "CGST", rs(half)],
                 ["", "", "", "SGST", rs(bill.gst_total - half)], ["", "", "", "Total", rs(bill.total)]]
        table = Table(rows, colWidths=[110, 50, 70, 50, 80])
        table.setStyle(TableStyle([("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                                   ("LINEBELOW", (0, 0), (-1, 0), 0.5, colors.black),
                                   ("FONTNAME", (-2, -1), (-1, -1), "Helvetica-Bold"),
                                   ("ALIGN", (1, 0), (-1, -1), "RIGHT")]))
        SimpleDocTemplate(str(path), pagesize=A5, title=f"Bill {bill.bill_no}").build([*head, Spacer(1, 8), table])
        return path
    except Exception:
        log.exception("PDF for bill %s failed", bill.id)
        return None


def bill_items(bill: Bill) -> list[BillItem]:
    from sqlalchemy.orm import object_session

    return list(object_session(bill).scalars(select(BillItem).where(BillItem.bill_id == bill.id).order_by(BillItem.id)))


SYSTEM_PROMPT = """- Sales: "bill banao / 2 maggi aur 1 atta becha" -> create_bill with every item and quantity.
  "Sharma ke khate me / udhaar pe" -> on_credit=true with customer_name. The PDF goes to the owner automatically.
  "bill 12 cancel karo / galat bill / maal wapas" -> cancel_bill (waits for YES).
"""

TOOLS = [create_bill, cancel_bill]
APPROVAL_RULES = {"cancel_bill": needs_approval_cancel_bill}
