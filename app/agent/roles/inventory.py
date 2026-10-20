"""Shopkeeper inventory tools: products and stock. Same contract as shopkeeper.py tools."""

import re
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agent.action_log import log_action
from app.agent.roles.shopkeeper import find_named, parse_amount, rupees
from app.db.models import Product

GST_SLABS = {Decimal(r) for r in ("0", "5", "18", "40")}  # GST 2.0, in force since 22 Sep 2025
MAX_QTY = Decimal("100000")


def parse_qty(value: Any, allow_zero: bool = False) -> Decimal | None:
    """A positive quantity (or 0 if allowed) with at most 3 decimals (kg, litre), else None."""
    try:
        qty = Decimal(str(value).replace(",", "").strip())
    except InvalidOperation:
        return None
    if not qty.is_finite():  # NaN must be refused before any comparison (NaN < 0 raises)
        return None
    too_small = qty < 0 if allow_zero else qty <= 0
    if too_small or qty > MAX_QTY or qty.as_tuple().exponent < -3:
        return None
    return qty


def normalise(name: str) -> str:
    """Product names: lowercase, single spaces. No honorific stripping ("Sabji" stays "sabji")."""
    return re.sub(r"\s+", " ", name.strip().lower())


def parse_gst(value: Any) -> Decimal | None:
    try:
        rate = Decimal(str(value).replace("%", "").strip())
    except InvalidOperation:
        return None
    return rate if rate.is_finite() and rate in GST_SLABS else None


def qty_text(qty: Decimal) -> str:
    return format(qty.normalize(), "f")


def products_of(session: Session, owner_id: int) -> list[Product]:
    return list(session.scalars(select(Product).where(Product.owner_id == owner_id)))


def find_product(session: Session, owner_id: int, name: str) -> tuple[Product | None, list[str]]:
    return find_named(products_of(session, owner_id), name, norm=normalise)


def ask_product(options: list[str]) -> dict:
    return {"ok": False, "ambiguous": options, "message": f"Not saved. Which product do you mean: {', '.join(options)}?"}


def describe(p: Product) -> str:
    return f"{p.name}: {qty_text(p.stock)} {p.unit} in stock, {rupees(p.price)} each (GST {qty_text(p.gst_rate)}%)"


# --- tools -------------------------------------------------------------------


def add_product(
    session: Session, owner_id: int, product_name: str, price: Any = None, gst_rate: Any = None,
    unit: str | None = None, opening_stock: Any = None, reorder_level: Any = None, new_product: bool = False,
) -> dict:
    """Add a new product, or update the price / GST rate / unit / reorder level of an existing one.
    price is the MRP including GST. gst_rate is one of 0, 5, 18, 40. opening_stock only applies to a new product;
    use add_stock for stock received later. Set new_product=true only after the owner confirms a similar-named
    product is different."""
    if not normalise(product_name or ""):
        return {"ok": False, "message": "Please tell me the product name."}
    fields: dict[str, Any] = {}
    checks = {
        "price": (price, parse_amount, "price must be more than 0 with max 2 decimals"),
        "gst_rate": (gst_rate, parse_gst, "GST rate must be 0, 5, 18 or 40"),
        "reorder_level": (reorder_level, lambda v: parse_qty(v, allow_zero=True), "reorder level must be 0 or more"),
        "stock": (opening_stock, lambda v: parse_qty(v, allow_zero=True), "opening stock must be 0 or more"),
    }
    for key, (value, parse, error) in checks.items():
        if value is not None and str(value).strip() != "":
            parsed = parse(value)
            if parsed is None:
                return {"ok": False, "message": f"Not saved: {error}."}
            fields[key] = parsed
    if unit:
        fields["unit"] = unit.strip().lower()[:10]

    if new_product:  # exact name still updates the existing product (names are unique per shop)
        target = normalise(product_name)
        product, options = next((p for p in products_of(session, owner_id) if normalise(p.name) == target), None), []
    else:
        product, options = find_product(session, owner_id, product_name)
    if options:
        return ask_product(options)
    if product is None:
        if "price" not in fields:
            return {"ok": False, "message": f"Not saved: what is the price (MRP) of {product_name}?"}
        product = Product(owner_id=owner_id, name=normalise(product_name).title(), **{
            "unit": "pcs", "gst_rate": Decimal("0"), "stock": Decimal("0"), "reorder_level": Decimal("0"), **fields})
        session.add(product)
        verb = "Added"
    else:
        if fields.pop("stock", 0) > 0:
            return {"ok": False, "message": f"Not saved: {product.name} already exists. Use add_stock for new stock."}
        for key, value in fields.items():
            setattr(product, key, value)
        verb = "Updated"
    session.flush()
    result = {"ok": True, "product": product.name, "message": f"{verb} {describe(product)}."}
    log_action(session, owner_id, "add_product", {"product_name": product_name, **{k: str(v) for k, v in fields.items()}}, result)
    return result


def add_stock(session: Session, owner_id: int, product_name: str, qty: Any) -> dict:
    """Record stock received for an existing product."""
    amount = parse_qty(qty)
    if amount is None:
        return {"ok": False, "message": "Not saved: quantity must be more than 0 (max 3 decimals)."}
    product, options = find_product(session, owner_id, product_name)
    if options:
        return ask_product(options)
    if product is None:
        return {"ok": False, "message": f"Not saved: no product named '{product_name}'. Add it with its price first."}
    session.refresh(product, with_for_update=True)  # latest stock under a row lock, so a bill at the same time is not lost
    product.stock += amount
    session.flush()
    result = {"ok": True, "product": product.name, "stock": str(product.stock),
              "message": f"Added {qty_text(amount)} {product.unit} of {product.name}. Now {qty_text(product.stock)} in stock."}
    log_action(session, owner_id, "add_stock", {"product_name": product_name, "qty": str(amount)}, result)
    return result


def check_stock(session: Session, owner_id: int, product_name: str | None = None) -> dict:
    """Stock of one product, or (no name) every product at or below its reorder level."""
    if product_name:
        product, options = find_product(session, owner_id, product_name)
        if options:
            return ask_product(options)
        if product is None:
            return {"ok": False, "message": f"No product named '{product_name}'."}
        result = {"ok": True, "message": describe(product) + "."}
    else:
        low = [p for p in products_of(session, owner_id) if p.reorder_level > 0 and p.stock <= p.reorder_level]
        low.sort(key=lambda p: p.name)
        lines = [f"{p.name}: {qty_text(p.stock)} {p.unit} (reorder at {qty_text(p.reorder_level)})" for p in low]
        result = {"ok": True, "low_stock": [p.name for p in low],
                  "message": "Low stock:\n" + "\n".join(lines) if lines else "All products are above their reorder level."}
    log_action(session, owner_id, "check_stock", {"product_name": product_name}, result)
    return result


SYSTEM_PROMPT = """- Products: "naya item / price set karo" -> add_product (price is MRP incl. GST; GST 0/5/18/40).
  "maal aaya / stock aaya" -> add_stock. "kitna bacha / stock" -> check_stock; with no name it lists low stock.
"""

TOOLS = [add_product, add_stock, check_stock]
