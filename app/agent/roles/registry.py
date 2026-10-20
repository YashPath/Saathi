"""Role name -> role pack."""

from decimal import Decimal

from app.agent.roles import billing, dashboard_link, inventory, messaging, professor, shopkeeper, student, summary
from app.agent.roles.base import Role
from app.config import get_settings


def _big_payment(_session, _user_id, args: dict) -> bool:
    amount = shopkeeper.parse_amount(args.get("amount"))
    return amount is not None and amount > Decimal(get_settings().approval_threshold)


SHOP_MODULES = (shopkeeper, inventory, billing, messaging, summary, dashboard_link)

ROLES = {
    "shopkeeper": Role("shopkeeper", "".join(m.SYSTEM_PROMPT for m in SHOP_MODULES),
                       [t for m in SHOP_MODULES for t in m.TOOLS],
                       approval_rules={"record_payment": _big_payment, **messaging.APPROVAL_RULES,
                                       **billing.APPROVAL_RULES}),
    "professor": Role("professor", professor.SYSTEM_PROMPT + dashboard_link.SYSTEM_PROMPT,
                      professor.TOOLS + dashboard_link.TOOLS, approval_rules=professor.APPROVAL_RULES),
    "student": Role("student", student.SYSTEM_PROMPT + dashboard_link.SYSTEM_PROMPT, student.TOOLS + dashboard_link.TOOLS),
}


def get_role(name: str) -> Role:
    return ROLES[name]
