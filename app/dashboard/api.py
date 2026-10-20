"""Read-only dashboard API. Every query is limited to the signed-in user; all changes go through the agent."""

from collections.abc import Iterator
from datetime import timedelta
from decimal import Decimal

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.agent.roles.professor import attendance_percentages, percent
from app.agent.roles.student import enrolments
from app.agent.roles.summary import summary_data, today_ist
from app.dashboard.auth import read_token
from app.db.models import ActionLog, Bill, ClassGroup, Customer, LedgerEntry, Mark, User
from app.db.session import get_session

router = APIRouter(prefix="/api")


def db() -> Iterator[Session]:
    with get_session() as session:
        yield session


def current_user(authorization: str | None = Header(default=None), session: Session = Depends(db)) -> User:
    token = (authorization or "").removeprefix("Bearer ").strip()
    claims = read_token(token)
    user = session.get(User, claims[0]) if claims else None
    if user is None or user.token_version != claims[1]:  # logged out since this link was made
        raise HTTPException(status_code=401, detail="Link expired or invalid. Ask Saathi for a new dashboard link.")
    return user


def need_role(user: User, role: str) -> None:
    if user.role != role:
        raise HTTPException(status_code=403, detail=f"Only for {role}s.")


def money(x) -> float:
    return float(Decimal(x).quantize(Decimal("0.01")))


@router.get("/me")
def me(user: User = Depends(current_user)) -> dict:
    return {"name": user.name, "role": user.role}


@router.get("/shop")
def shop(user: User = Depends(current_user), session: Session = Depends(db)) -> dict:
    need_role(user, "shopkeeper")
    today = summary_data(session, user.id)
    start, _ = today_ist()
    week = []
    for i in range(6, -1, -1):
        day_start, day_end = start - timedelta(days=i), start - timedelta(days=i - 1)
        total = session.scalar(select(func.coalesce(func.sum(Bill.total), 0)).where(
            Bill.owner_id == user.id, Bill.cancelled_at.is_(None), Bill.created_at >= day_start, Bill.created_at < day_end))
        week.append({"day": day_start.strftime("%a %d"), "sales": money(total)})
    balance = func.sum(LedgerEntry.amount)
    dues = session.execute(select(Customer.name, balance).join(LedgerEntry, LedgerEntry.customer_id == Customer.id)
                           .where(Customer.owner_id == user.id).group_by(Customer.id, Customer.name)
                           .having(balance > 0).order_by(balance.desc()).limit(10)).all()
    bills = session.execute(select(Bill.bill_no, Bill.total, Bill.on_credit, Bill.created_at, Customer.name,
                                   Bill.cancelled_at)
                            .outerjoin(Customer, Customer.id == Bill.customer_id).where(Bill.owner_id == user.id)
                            .order_by(Bill.id.desc()).limit(10)).all()
    return {
        "today": {k: today[k] for k in ("bills", "customers_with_dues")} | {
            k: money(today[k]) for k in ("sales", "cash", "credit_sales", "received", "credit_given", "total_dues")},
        "low_stock": today["low_stock"],
        "week": week,
        "dues": [{"customer": n, "balance": money(b)} for n, b in dues],
        "bills": [{"no": no, "total": money(t), "on_credit": c, "customer": cust, "at": at.isoformat(),
                   "cancelled": cancelled is not None} for no, t, c, at, cust, cancelled in bills],
    }


@router.get("/classes")
def classes(user: User = Depends(current_user), session: Session = Depends(db)) -> dict:
    need_role(user, "professor")
    out = []
    for group in session.scalars(select(ClassGroup).where(ClassGroup.owner_id == user.id).order_by(ClassGroup.name)):
        stats = attendance_percentages(session, group)
        marked = [(s, p, d) for s, p, d in stats if d]
        overall = percent(sum(p for _, p, _ in marked), sum(d for _, _, d in marked)) if marked else None
        averages = session.execute(select(Mark.assessment, func.avg(Mark.score / Mark.max_marks * 100))
                                   .where(Mark.class_id == group.id).group_by(Mark.assessment)
                                   .order_by(Mark.assessment)).all()
        out.append({
            "name": group.name,
            "students": len(stats),
            "attendance": float(overall) if overall is not None else None,
            "low_attendance": [{"roll": s.roll_no, "name": s.name, "percent": float(percent(p, d))}
                               for s, p, d in marked if percent(p, d) < 75],
            "assessments": [{"name": a, "average_percent": round(float(v), 1)} for a, v in averages],
        })
    return {"classes": out}


@router.get("/student")
def student(user: User = Depends(current_user), session: Session = Depends(db)) -> dict:
    need_role(user, "student")
    out = []
    for me_in_class, group in enrolments(session, user.id):
        p, d = next((p, d) for s, p, d in attendance_percentages(session, group) if s.id == me_in_class.id)
        marks = session.execute(select(Mark.assessment, Mark.score, Mark.max_marks)
                                .where(Mark.student_id == me_in_class.id).order_by(Mark.assessment)).all()
        out.append({"class": group.name, "roll": me_in_class.roll_no, "present": p, "days": d,
                    "attendance": float(percent(p, d)) if d else None,
                    "marks": [{"name": a, "score": float(sc), "out_of": float(mx)} for a, sc, mx in marks]})
    return {"classes": out}


@router.get("/activity")
def activity(user: User = Depends(current_user), session: Session = Depends(db)) -> dict:
    """The agent's recent actions for this user: what Saathi did, when, and whether it went through."""
    rows = session.scalars(select(ActionLog).where(ActionLog.user_id == user.id)
                           .order_by(ActionLog.id.desc()).limit(50)).all()
    return {"actions": [{"tool": r.tool_name, "status": r.status, "at": r.created_at.isoformat(),
                         "summary": ((r.output_json or {}).get("message") or "")[:160]} for r in rows]}
