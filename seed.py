"""Create the demo shopkeeper and professor users. Safe to run many times."""

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db.models import User
from app.mobile.accounts import hash_secret, valid_pin


def seed(session: Session, settings: Settings) -> list[str]:
    """Insert seed users whose phone is set and not already present. Returns created phones."""
    wanted = [
        (settings.seed_shopkeeper_phone, settings.seed_shopkeeper_name, "shopkeeper"),
        (settings.seed_professor_phone, settings.seed_professor_name, "professor"),
    ]
    created = []
    for phone, name, role in wanted:
        if not phone:
            print(f"Skipping {role}: no phone set in .env")
            continue
        if session.scalar(select(User).where(User.phone == phone)) is None:
            user = User(phone=phone, name=name, role=role)
            if settings.seed_pin:
                if not valid_pin(settings.seed_pin):
                    raise SystemExit("SEED_PIN must be exactly 6 digits.")
                user.pin_hash = hash_secret(settings.seed_pin)
            session.add(user)
            created.append(phone)
    session.commit()
    return created


if __name__ == "__main__":
    from app.db.session import SessionLocal

    with SessionLocal() as s:
        print("Created:", seed(s, get_settings()) or "nothing (already seeded)")
