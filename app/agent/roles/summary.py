"""Shopkeeper daily summary: today's sales, money in and out, dues and low stock."""

from datetime import datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.agent.action_log import log_action
from app.agent.roles.inventory import products_of, qty_text
from app.agent.roles.shopkeeper import rupees
from app.db.models import ActionLog, Bill, Customer, LedgerEntry

IST = ZoneInfo("Asia/Kolkata")


def today_ist(now: datetime | None = None) -> tuple[datetime, datetime]:
    """Start and end of the current day in India, as aware datetimes."""
    day = (now or datetime.now(IST)).astimezone(IST).date()
    start = datetime.combine(day, time.min, IST)
    return start, start + timedelta(days=1)


def _sum_logged(session: Session, owner_id: int, tool: str, start: datetime, end: datetime) -> Decimal:
    """Amounts of today's completed (not undone) ledger actions; the log knows payments from undo entries."""
    rows = session.scalars(select(ActionLog.output_json).where(
        ActionLog.user_id == owner_id, ActionLog.tool_name == tool, ActionLog.status == "done",
        ActionLog.created_at >= start, ActionLog.created_at < end))
    return sum((Decimal(r["amount"]) for r in rows), Decimal("0"))


def summary_data(session: Session, owner_id: int) -> dict:
    """Today's numbers without logging anything (the dashboard reads this on every refresh)."""
    start, end = today_ist()
    bills = session.execute(select(Bill.total, Bill.on_credit).where(
        Bill.owner_id == owner_id, Bill.cancelled_at.is_(None), Bill.created_at >= start, Bill.created_at < end)).all()
    sales = sum((t for t, _ in bills), Decimal("0"))
    credit_sales = sum((t for t, c in bills if c), Decimal("0"))
    received = _sum_logged(session, owner_id, "record_payment", start, end)
    credit_given = _sum_logged(session, owner_id, "add_credit", start, end)
    balance = func.sum(LedgerEntry.amount)
    dues = session.scalars(select(balance).join(Customer, Customer.id == LedgerEntry.customer_id)
                           .where(Customer.owner_id == owner_id).group_by(Customer.id).having(balance > 0)).all()
    total_dues = sum(dues, Decimal("0"))
    low = sorted((p for p in products_of(session, owner_id) if p.reorder_level > 0 and p.stock <= p.reorder_level),
                 key=lambda p: p.name)

    lines = [
        f"Today's summary ({start.strftime('%d %b')})",
        f"Sales: {len(bills)} bills, {rupees(sales)} (cash {rupees(sales - credit_sales)}, credit {rupees(credit_sales)})",
        f"Payments received: {rupees(received)}",
        f"Credit given: {rupees(credit_given)} (includes credit bills)",
        f"Total dues: {rupees(total_dues)} from {len(dues)} customers",
        "Low stock: " + (", ".join(f"{p.name} ({qty_text(p.stock)} {p.unit})" for p in low) or "none"),
    ]
    return {"ok": True, "bills": len(bills), "sales": str(sales), "cash": str(sales - credit_sales),
            "credit_sales": str(credit_sales), "received": str(received), "credit_given": str(credit_given),
            "total_dues": str(total_dues), "customers_with_dues": len(dues),
            "low_stock": [{"name": p.name, "stock": qty_text(p.stock), "unit": p.unit} for p in low],
            "message": "\n".join(lines)}


def daily_summary(session: Session, owner_id: int) -> dict:
    """Today's summary: sales (cash and credit), payments received, credit given, total dues and low stock."""
    data = summary_data(session, owner_id)
    result = {k: data[k] for k in ("ok", "bills", "sales", "received", "credit_given", "total_dues", "message")}
    log_action(session, owner_id, "daily_summary", {}, result)
    return result


SYSTEM_PROMPT = """- "aaj ka hisaab / summary" -> daily_summary. It is also sent automatically every evening.
"""

TOOLS = [daily_summary]
