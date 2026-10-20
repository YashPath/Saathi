"""Measure Saathi with the real model: task success, intent accuracy and response time.

Usage (needs GOOGLE_API_KEY and a THROWAWAY database — every case drops and recreates all tables):
    EVAL_DATABASE_URL=postgresql+psycopg://saathi:saathi@localhost:5432/saathi_eval python eval/run_eval.py
Writes eval/results/<time>.md and .csv. Each case runs on a fresh copy of the same demo shop, class and student.

A case passes when every expected tool was called with matching arguments (a call that waited for approval
counts) and no forbidden tool actually ran. Intent is correct when the first tool the agent chose is the
expected first tool (or, for cases that expect nothing, when no write tool ran).
"""

import argparse
import csv
import json
import os
import statistics
import sys
import time
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import create_engine, select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.agent import outbox  # noqa: E402
from app.agent.approval import REQUEST_TOOL  # noqa: E402
from app.agent.graph import handle_message  # noqa: E402
from app.agent.roles import inventory, messaging, professor, shopkeeper  # noqa: E402
from app.db.models import ActionLog, Base, User  # noqa: E402

PHONES = {"shopkeeper": "910000000001", "professor": "910000000002", "student": "919811100001"}
READ_ONLY = {"get_balance", "list_dues", "check_stock", "daily_summary", "attendance_report", "my_attendance",
             "marks_summary", "dashboard_link", "ask_doubt", "name_check", REQUEST_TOOL}
SEED_ENV = {"DASHBOARD_SECRET": "eval-only-secret-0123456789abcdefghij"}  # so dashboard_link can be scored


def seed(session: Session) -> None:
    """The same small world for every case."""
    shop = User(phone=PHONES["shopkeeper"], name="Saathi Demo Store", role="shopkeeper", upi_id="demostore@okaxis")
    prof = User(phone=PHONES["professor"], name="Prof Demo", role="professor")
    session.add_all([shop, prof])
    session.flush()
    shopkeeper.add_credit(session, shop.id, "Sharma", 1200)
    shopkeeper.add_credit(session, shop.id, "Gupta", 300)
    messaging.set_customer_phone(session, shop.id, "Sharma", "9811122233")
    inventory.add_product(session, shop.id, "Maggi", price="14", gst_rate="18", opening_stock="40", reorder_level="10")
    inventory.add_product(session, shop.id, "Atta", price="52.50", gst_rate="5", opening_stock="20", unit="kg", reorder_level="5")
    inventory.add_product(session, shop.id, "Parle G", price="10", opening_stock="8", reorder_level="10")
    messaging.add_supplier(session, shop.id, "Ramesh Traders", "9876543210")
    professor.add_class(session, prof.id, "CSE-3")
    professor.add_students(session, prof.id, "CSE-3", [
        {"roll_no": 1, "name": "Ravi", "phone": PHONES["student"][2:]}, {"roll_no": 2, "name": "Neha"},
        {"roll_no": 3, "name": "Aman"}, {"roll_no": 4, "name": "Zoya"}, {"roll_no": 5, "name": "Kabir"}])
    professor.mark_attendance(session, prof.id, "CSE-3", [2])
    student = session.scalar(select(User).where(User.phone == PHONES["student"]))
    from app.agent.roles.student import join_class

    join_class(session, student.id, "CSE-3")  # Ravi accepted the invite
    session.query(ActionLog).delete()  # setup actions are not the agent's
    session.commit()
    outbox.take(session)


def arg_matches(expected: str, actual) -> bool:
    """Numbers compare by value; text matches when the expected word appears (case-insensitive)."""
    try:
        return Decimal(str(expected)) == Decimal(str(actual))
    except (InvalidOperation, ValueError):
        return str(expected).lower() in str(actual).lower()


def calls_of(session: Session, user_id: int) -> list[tuple[str, dict, str]]:
    """(tool, args, status) in order; a parked approval counts as a call of the tool it parked."""
    out = []
    for row in session.scalars(select(ActionLog).where(ActionLog.user_id == user_id).order_by(ActionLog.id)):
        if row.tool_name == REQUEST_TOOL:
            out.append((row.input_json["tool"], row.input_json["args"], "approval"))
        else:
            out.append((row.tool_name, row.input_json or {}, row.status))
    return out


def score(case: dict, calls: list[tuple[str, dict, str]]) -> tuple[bool, bool, str]:
    """(success, intent correct, note)."""
    missing = []
    for tool, args in case["expect"]:
        ok = any(t == tool and all(arg_matches(v, a.get(k, "")) for k, v in args.items()) for t, a, _ in calls)
        if not ok:
            missing.append(tool)
    ran = {t for t, _, status in calls if status == "done"}
    forbidden = sorted(set(case.get("forbid", [])) & ran)
    writes = [t for t, _, _ in calls if t not in READ_ONLY]
    first = next((t for t, _, _ in calls if t != "name_check"), None)
    intent = first == case["expect"][0][0] if case["expect"] else not (set(writes) & set(case.get("forbid", [])))
    note = "; ".join(filter(None, [f"missing {missing}" if missing else "", f"forbidden ran {forbidden}" if forbidden else ""]))
    return not missing and not forbidden, intent, note or "ok"


def run(cases: list[dict], engine, llm_factory) -> list[dict]:
    rows = []
    for case in cases:
        Base.metadata.drop_all(engine)
        Base.metadata.create_all(engine)
        with Session(engine) as s:
            seed(s)
        times, replies = [], []
        with Session(engine) as s:
            user_id = s.scalar(select(User.id).where(User.phone == PHONES[case["role"]]))
            llm = llm_factory(case)
            for text in case["messages"]:
                t0 = time.perf_counter()
                replies.append(handle_message(s, PHONES[case["role"]], text, llm))
                s.commit()
                times.append(time.perf_counter() - t0)
            calls = calls_of(s, user_id)
        success, intent, note = score(case, calls)
        rows.append({"id": case["id"], "role": case["role"], "lang": case["lang"], "success": success, "intent": intent,
                     "seconds": round(statistics.mean(times), 2), "tools": " > ".join(t for t, _, _ in calls),
                     "note": note, "reply": replies[-1][:200].replace("\n", " ")})
    return rows


def pct(rows, key) -> str:
    return f"{100 * sum(r[key] for r in rows) / len(rows):.1f}% ({sum(r[key] for r in rows)}/{len(rows)})" if rows else "-"


def report(rows: list[dict]) -> str:
    secs = sorted(r["seconds"] for r in rows)
    p95 = secs[min(len(secs) - 1, int(0.95 * len(secs)))]
    lines = ["# Saathi evaluation", "", f"Run: {datetime.now():%d %b %Y %H:%M}, {len(rows)} cases", "",
             f"- Task success: {pct(rows, 'success')}", f"- Intent accuracy: {pct(rows, 'intent')}",
             f"- Response time per message: average {statistics.mean(secs):.2f} s, p95 {p95:.2f} s", "",
             "| Group | Cases | Task success | Intent accuracy |", "|---|---|---|---|"]
    for key in ("role", "lang"):
        for value in sorted({r[key] for r in rows}):
            group = [r for r in rows if r[key] == value]
            lines.append(f"| {key}: {value} | {len(group)} | {pct(group, 'success')} | {pct(group, 'intent')} |")
    lines += ["", "## Failures", ""] + [f"- {r['id']} ({r['lang']}): {r['note']} — tools: {r['tools'] or 'none'}"
                                         for r in rows if not r["success"]] or ["- none"]
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--cases", default=str(Path(__file__).parent / "cases.jsonl"))
    parser.add_argument("--only", help="comma-separated case ids")
    args = parser.parse_args()
    url = os.environ.get("EVAL_DATABASE_URL")
    if not url:
        sys.exit("Set EVAL_DATABASE_URL to a throwaway database (all its tables are dropped).")
    cases = [json.loads(line) for line in Path(args.cases).read_text(encoding="utf-8").splitlines() if line.strip()]
    if args.only:
        cases = [c for c in cases if c["id"] in args.only.split(",")]
    from app.agent.llm import get_llm
    from app.config import get_settings

    if len(get_settings().dashboard_secret) < 32:
        get_settings().dashboard_secret = SEED_ENV["DASHBOARD_SECRET"]
    llm = get_llm()
    rows = run(cases, create_engine(url), lambda case: llm)
    out = Path(__file__).parent / "results"
    out.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    (out / f"{stamp}.md").write_text(report(rows), encoding="utf-8")
    with open(out / f"{stamp}.csv", "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    print(report(rows))
    print(f"Saved eval/results/{stamp}.md and .csv")


if __name__ == "__main__":
    main()
