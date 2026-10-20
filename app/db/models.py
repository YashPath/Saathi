"""SQLAlchemy models."""

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import JSON, CheckConstraint, Date, DateTime, ForeignKey, Numeric, String, Text, UniqueConstraint, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class User(TimestampMixin, Base):
    __tablename__ = "users"
    __table_args__ = (CheckConstraint("role IN ('shopkeeper', 'professor', 'student')", name="ck_users_role"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    phone: Mapped[str] = mapped_column(String(20), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(100))
    role: Mapped[str] = mapped_column(String(20))
    gstin: Mapped[str | None] = mapped_column(String(15))  # shown on bills when set
    upi_id: Mapped[str | None] = mapped_column(String(100))  # for payment reminders
    token_version: Mapped[int] = mapped_column(default=0, server_default="0")  # +1 = every login token stops working
    pin_hash: Mapped[str | None] = mapped_column(String(200))  # app login; scrypt "salt$hash"
    setup_code_hash: Mapped[str | None] = mapped_column(String(200))  # one-time code a professor gives a student
    setup_code_expires: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failed_logins: Mapped[int] = mapped_column(default=0, server_default="0")
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Customer(Base):
    __tablename__ = "customers"
    __table_args__ = (UniqueConstraint("owner_id", "name", name="uq_customers_owner_name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    name: Mapped[str] = mapped_column(String(100))
    phone: Mapped[str | None] = mapped_column(String(20))


class LedgerEntry(TimestampMixin, Base):
    __tablename__ = "ledger_entries"

    id: Mapped[int] = mapped_column(primary_key=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"), index=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2))  # +credit, -payment
    note: Mapped[str | None] = mapped_column(Text)


class ActionLog(TimestampMixin, Base):
    __tablename__ = "action_logs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('done', 'pending_approval', 'rejected', 'undone')", name="ck_action_logs_status"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    tool_name: Mapped[str] = mapped_column(String(100))
    input_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    output_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(20))


class PendingAction(Base):
    __tablename__ = "pending_actions"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    tool_name: Mapped[str] = mapped_column(String(100))
    args_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    summary_text: Mapped[str] = mapped_column(Text)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Message(TimestampMixin, Base):
    """Conversation history, one row per turn."""

    __tablename__ = "messages"
    __table_args__ = (CheckConstraint("role IN ('user', 'assistant')", name="ck_messages_role"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    role: Mapped[str] = mapped_column(String(20))
    content: Mapped[str] = mapped_column(Text)


class Supplier(TimestampMixin, Base):
    __tablename__ = "suppliers"
    __table_args__ = (UniqueConstraint("owner_id", "name", name="uq_suppliers_owner_name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    name: Mapped[str] = mapped_column(String(100))
    phone: Mapped[str] = mapped_column(String(20))


class Product(TimestampMixin, Base):
    __tablename__ = "products"
    __table_args__ = (
        UniqueConstraint("owner_id", "name", name="uq_products_owner_name"),
        CheckConstraint("price > 0", name="ck_products_price"),
        CheckConstraint("stock >= 0", name="ck_products_stock"),  # last line of defence against overselling
        CheckConstraint("gst_rate IN (0, 5, 18, 40)", name="ck_products_gst"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    name: Mapped[str] = mapped_column(String(100))
    unit: Mapped[str] = mapped_column(String(10), default="pcs")
    price: Mapped[Decimal] = mapped_column(Numeric(12, 2))  # MRP, GST included
    gst_rate: Mapped[Decimal] = mapped_column(Numeric(5, 2), default=Decimal("0"))
    stock: Mapped[Decimal] = mapped_column(Numeric(12, 3), default=Decimal("0"))
    reorder_level: Mapped[Decimal] = mapped_column(Numeric(12, 3), default=Decimal("0"))
    supplier_id: Mapped[int | None] = mapped_column(ForeignKey("suppliers.id"))


class Bill(TimestampMixin, Base):
    __tablename__ = "bills"
    __table_args__ = (UniqueConstraint("owner_id", "bill_no", name="uq_bills_owner_no"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    bill_no: Mapped[int]
    customer_id: Mapped[int | None] = mapped_column(ForeignKey("customers.id"))
    on_credit: Mapped[bool] = mapped_column(default=False)
    taxable: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    gst_total: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    total: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))  # rows are never deleted


class BillItem(Base):
    """A snapshot of the line as sold: later price changes never alter an old bill."""

    __tablename__ = "bill_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    bill_id: Mapped[int] = mapped_column(ForeignKey("bills.id"), index=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"))
    name: Mapped[str] = mapped_column(String(100))
    qty: Mapped[Decimal] = mapped_column(Numeric(12, 3))
    unit: Mapped[str] = mapped_column(String(10))
    price: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    gst_rate: Mapped[Decimal] = mapped_column(Numeric(5, 2))
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    gst_amount: Mapped[Decimal] = mapped_column(Numeric(12, 2))


class ClassGroup(TimestampMixin, Base):
    __tablename__ = "classes"
    __table_args__ = (UniqueConstraint("owner_id", "name", name="uq_classes_owner_name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)  # the professor
    name: Mapped[str] = mapped_column(String(50))


class Student(Base):
    __tablename__ = "students"
    __table_args__ = (
        UniqueConstraint("class_id", "roll_no", name="uq_students_class_roll"),
        CheckConstraint("roll_no > 0", name="ck_students_roll"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    class_id: Mapped[int] = mapped_column(ForeignKey("classes.id"), index=True)
    roll_no: Mapped[int]
    name: Mapped[str] = mapped_column(String(100))
    phone: Mapped[str | None] = mapped_column(String(20))
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), index=True)  # their login (invited)
    joined_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))  # set when THEY accept the invite


class Attendance(Base):
    __tablename__ = "attendance"
    __table_args__ = (UniqueConstraint("student_id", "day", name="uq_attendance_student_day"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    class_id: Mapped[int] = mapped_column(ForeignKey("classes.id"), index=True)
    student_id: Mapped[int] = mapped_column(ForeignKey("students.id"), index=True)
    day: Mapped[date] = mapped_column(Date)
    present: Mapped[bool]


class Mark(Base):
    __tablename__ = "marks"
    __table_args__ = (
        UniqueConstraint("student_id", "assessment", name="uq_marks_student_assessment"),
        CheckConstraint("max_marks > 0 AND score >= 0 AND score <= max_marks", name="ck_marks_range"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    class_id: Mapped[int] = mapped_column(ForeignKey("classes.id"), index=True)
    student_id: Mapped[int] = mapped_column(ForeignKey("students.id"), index=True)
    assessment: Mapped[str] = mapped_column(String(50))
    max_marks: Mapped[Decimal] = mapped_column(Numeric(6, 2))
    score: Mapped[Decimal] = mapped_column(Numeric(6, 2))


class Assignment(TimestampMixin, Base):
    __tablename__ = "assignments"

    id: Mapped[int] = mapped_column(primary_key=True)
    class_id: Mapped[int] = mapped_column(ForeignKey("classes.id"), index=True)
    title: Mapped[str] = mapped_column(String(100))
    due_date: Mapped[date] = mapped_column(Date)
    reminded: Mapped[bool] = mapped_column(default=False)


class Note(TimestampMixin, Base):
    """Lecture notes; the text is also chunked into the vector store for search."""

    __tablename__ = "notes"

    id: Mapped[int] = mapped_column(primary_key=True)
    class_id: Mapped[int] = mapped_column(ForeignKey("classes.id"), index=True)
    title: Mapped[str] = mapped_column(String(100))
    text: Mapped[str] = mapped_column(Text)


class Notification(TimestampMixin, Base):
    """Something for a user to see in the app: a message from another user or a scheduled job, or a file."""

    __tablename__ = "notifications"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    text: Mapped[str | None] = mapped_column(Text)
    file_path: Mapped[str | None] = mapped_column(String(300))
    filename: Mapped[str | None] = mapped_column(String(120))
    seen: Mapped[bool] = mapped_column(default=False)
