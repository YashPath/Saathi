"""App accounts: phone + 6-digit PIN. Students claim the login a professor created with a one-time setup code."""

import hashlib
import hmac
import re
import secrets
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agent.roles.messaging import parse_phone
from app.config import get_settings
from app.dashboard.auth import make_token
from app.db.models import User

MAX_FAILS = 5
LOCK_FOR = timedelta(minutes=15)
CODE_VALID = timedelta(days=7)
SELF_SIGNUP_ROLES = {"shopkeeper", "professor"}  # students only through a professor's setup code


class AuthError(Exception):
    """A login or sign-up the user can fix; the message is safe to show."""


def now() -> datetime:
    return datetime.now(timezone.utc)


def hash_secret(value: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(value.encode(), salt=salt, n=2**14, r=8, p=1)
    return f"{salt.hex()}${digest.hex()}"


def check_secret(value: str, stored: str | None) -> bool:
    if not stored or "$" not in stored:
        return False
    salt, digest = stored.split("$", 1)
    actual = hashlib.scrypt(value.encode(), salt=bytes.fromhex(salt), n=2**14, r=8, p=1).hex()
    return hmac.compare_digest(actual, digest)


def valid_pin(pin: str) -> bool:
    return bool(re.fullmatch(r"\d{6}", pin or ""))


def new_setup_code(user: User) -> str:
    """Give a student a fresh one-time code (shown to their professor); replaces any older one."""
    code = f"{secrets.randbelow(10**6):06d}"
    user.setup_code_hash, user.setup_code_expires = hash_secret(code), now() + CODE_VALID
    return code


def token_for(user: User) -> str:
    return make_token(user.id, user.token_version, hours=get_settings().app_login_days * 24)


def _user(session: Session, phone: str) -> User | None:
    number = parse_phone(phone)
    if number is None:
        raise AuthError("Enter a valid 10-digit mobile number.")
    return session.scalar(select(User).where(User.phone == number).with_for_update())


def _guard(user: User) -> None:
    if user.locked_until and user.locked_until > now():
        raise AuthError("Too many wrong attempts. Try again in 15 minutes.")


def _failed(session: Session, user: User) -> None:
    user.failed_logins += 1
    if user.failed_logins >= MAX_FAILS:
        user.failed_logins, user.locked_until = 0, now() + LOCK_FOR
    session.commit()  # the count must survive the error raised next


def register(session: Session, phone: str, name: str, role: str, pin: str, setup_code: str | None = None) -> User:
    """Shopkeepers and professors sign up themselves; a student claims their login with the professor's code."""
    if not valid_pin(pin):
        raise AuthError("PIN must be exactly 6 digits.")
    user = _user(session, phone)
    if role == "student":
        if user is None or user.role != "student" or user.pin_hash:
            raise AuthError("Ask your professor to add you to a class first, then use the code they give you.")
        _guard(user)
        if not (setup_code and user.setup_code_expires and user.setup_code_expires > now()
                and check_secret(setup_code, user.setup_code_hash)):
            _failed(session, user)
            raise AuthError("That code is wrong or has expired. Ask your professor for a new one.")
    elif role in SELF_SIGNUP_ROLES:
        if user is not None:
            raise AuthError("This number already has an account. Log in instead.")
        name = (name or "").strip()
        if not 2 <= len(name) <= 100:
            raise AuthError("Enter your name or your shop's name.")
        user = User(phone=parse_phone(phone), name=name, role=role)
        session.add(user)
    else:
        raise AuthError("Choose shopkeeper, professor or student.")
    user.pin_hash = hash_secret(pin)
    user.setup_code_hash = user.setup_code_expires = None
    user.failed_logins, user.locked_until = 0, None
    session.flush()
    return user


def login(session: Session, phone: str, pin: str) -> User:
    user = _user(session, phone)
    if user is None or not user.pin_hash:
        raise AuthError("Wrong number or PIN.")  # same words either way: do not reveal who has an account
    _guard(user)
    if not check_secret(pin or "", user.pin_hash):
        _failed(session, user)
        raise AuthError("Wrong number or PIN.")
    user.failed_logins, user.locked_until = 0, None
    session.flush()
    return user
