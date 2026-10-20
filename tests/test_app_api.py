"""Mobile app API: accounts, chat, approvals, share cards, files, inbox, voice, logout."""

from datetime import timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.agent import graph
from app.agent.roles import inventory as inv
from app.agent.roles import messaging as msg
from app.agent.roles import professor as prof
from app.agent.roles import shopkeeper as shop
from app.config import get_settings
from app.dashboard import api as dash_api
from app.db.models import Notification, User
from app.mobile import accounts
from app.mobile import api as app_api
from tests.conftest import accept_invites, fake_llm, needs_db

pytestmark = needs_db
SHOP_PHONE, PROF_PHONE = "9811100001", "9811100002"


@pytest.fixture(autouse=True)
def configured(monkeypatch, tmp_path):
    monkeypatch.setattr(get_settings(), "dashboard_secret", "test-dashboard-secret-0123456789abcdef")
    monkeypatch.setattr(get_settings(), "bills_dir", str(tmp_path))


@pytest.fixture
def client(session):
    app = FastAPI()
    app.include_router(app_api.router)
    app.include_router(dash_api.router)

    def test_db():
        yield session
        session.flush()

    app.dependency_overrides[dash_api.db] = test_db
    return TestClient(app)


def script(monkeypatch, *steps):
    """Make the agent use a scripted model for the next turn."""
    monkeypatch.setattr(graph, "get_llm", lambda: fake_llm(*steps))


def sign_up(client, phone=SHOP_PHONE, role="shopkeeper", name="Yash Store", pin="123456", **extra):
    return client.post("/api/app/register", json={"phone": phone, "pin": pin, "role": role, "name": name, **extra})


def add_ravi(session):
    """Professor adds a student the way the agent would: tool call, then delivery of what it queued."""
    from app.agent.outbox import take
    from app.mobile.delivery import deliver

    professor = session.scalar(select(User).where(User.role == "professor"))
    prof.add_class(session, professor.id, "CSE-3")
    r = prof.add_students(session, professor.id, "CSE-3", [{"roll_no": 1, "name": "Ravi", "phone": "9822200001"}])
    deliver(session, professor, take(session))
    return r["message"].split("roll 1 Ravi: ")[1][:6], r["message"]


def auth(r):
    return {"Authorization": f"Bearer {r.json()['token']}"}


# --- accounts -----------------------------------------------------------------


def test_sign_up_and_log_in(client):
    r = sign_up(client, phone="+91 98111-00001")
    assert r.status_code == 200 and r.json()["user"] == {"name": "Yash Store", "role": "shopkeeper", "phone": "919811100001"}
    assert client.post("/api/app/login", json={"phone": SHOP_PHONE, "pin": "123456"}).status_code == 200
    assert client.get("/api/me", headers=auth(r)).json()["role"] == "shopkeeper"  # same token opens the dashboard API


@pytest.mark.parametrize("kwargs,detail", [
    ({"pin": "1234"}, "6 digits"), ({"phone": "123"}, "valid 10-digit"), ({"role": "admin"}, "Choose"),
    ({"role": "student"}, "Ask your professor"), ({"name": "A"}, "name"),
])
def test_sign_up_refusals(client, kwargs, detail):
    r = sign_up(client, **kwargs)
    assert r.status_code == 400 and detail in r.json()["detail"]


def test_duplicate_sign_up_refused(client):
    sign_up(client)
    assert "already has an account" in sign_up(client).json()["detail"]


def test_wrong_pin_locks_after_five(client, session):
    sign_up(client)
    for _ in range(5):
        assert client.post("/api/app/login", json={"phone": SHOP_PHONE, "pin": "000000"}).status_code == 401
    r = client.post("/api/app/login", json={"phone": SHOP_PHONE, "pin": "123456"})
    assert r.status_code == 401 and "15 minutes" in r.json()["detail"]
    user = session.scalar(select(User))
    user.locked_until = accounts.now() - timedelta(seconds=1)
    session.flush()
    assert client.post("/api/app/login", json={"phone": SHOP_PHONE, "pin": "123456"}).status_code == 200


def test_unknown_number_and_wrong_pin_look_the_same(client):
    sign_up(client)
    a = client.post("/api/app/login", json={"phone": "9899999999", "pin": "123456"}).json()["detail"]
    b = client.post("/api/app/login", json={"phone": SHOP_PHONE, "pin": "654321"}).json()["detail"]
    assert a == b == "Wrong number or PIN."


def test_student_claims_login_with_professors_code(client, session):
    p = auth(sign_up(client, phone=PROF_PHONE, role="professor", name="Prof Rao"))
    code, message = add_ravi(session)
    assert "App setup codes" in message
    assert sign_up(client, phone="9822200001", role="student", setup_code="000000").status_code == 400
    s = sign_up(client, phone="9822200001", role="student", setup_code=code, pin="222222")
    assert s.status_code == 200 and s.json()["user"]["role"] == "student"
    assert sign_up(client, phone="9822200001", role="student", setup_code=code, pin="333333").status_code == 400  # once
    inbox = client.get("/api/app/inbox", headers=auth(s)).json()["items"]
    assert 'Say "join CSE-3"' in inbox[0]["text"]
    assert client.get("/api/app/inbox", headers=auth(s)).json()["items"] == []  # each item once
    assert p  # professor still logged in


def test_expired_code_refused(client, session):
    sign_up(client, phone=PROF_PHONE, role="professor", name="Prof Rao")
    code, _ = add_ravi(session)
    session.scalar(select(User).where(User.role == "student")).setup_code_expires = accounts.now() - timedelta(1)
    session.flush()
    assert "expired" in sign_up(client, phone="9822200001", role="student", setup_code=code).json()["detail"]


def test_logout_everywhere(client):
    h = auth(sign_up(client))
    assert client.post("/api/app/logout", headers=h).json() == {"ok": True}
    assert client.get("/api/app/history", headers=h).status_code == 401


def test_routes_need_a_token(client):
    for path in ("/api/app/history", "/api/app/inbox"):
        assert client.get(path).status_code == 401
    assert client.post("/api/app/chat", json={"text": "hi"}).status_code == 401


# --- chat -----------------------------------------------------------------------


def test_chat_and_history(client, monkeypatch):
    h = auth(sign_up(client))
    script(monkeypatch, [("add_credit", {"customer_name": "Sharma", "amount": "500"})], "Sharma ka 500 likh diya.")
    r = client.post("/api/app/chat", json={"text": "Sharma ji ka 500 likh do"}, headers=h).json()
    assert r["reply"] == "Sharma ka 500 likh diya." and r["quick_replies"] == []
    msgs = client.get("/api/app/history", headers=h).json()["messages"]
    assert [m["role"] for m in msgs] == ["user", "assistant"]


def test_approval_gives_quick_replies(client, session, monkeypatch):
    h = auth(sign_up(client))
    shop.add_credit(session, session.scalar(select(User.id)), "Gupta", 9000)
    script(monkeypatch, [("record_payment", {"customer_name": "Gupta", "amount": "6000"})], "Confirm karein?")
    r = client.post("/api/app/chat", json={"text": "Gupta ne 6000 diye"}, headers=h).json()
    assert r["quick_replies"] == ["Haan", "Nahi"]
    r = client.post("/api/app/chat", json={"text": "Haan"}, headers=h).json()
    assert r["reply"].startswith("Recorded ₹6,000.00 payment") and r["quick_replies"] == []


def test_bill_pdf_comes_back_as_a_file(client, session, monkeypatch):
    h = auth(sign_up(client))
    inv.add_product(session, session.scalar(select(User.id)), "Maggi", price="14", opening_stock="10")
    script(monkeypatch, [("create_bill", {"items": [{"product_name": "Maggi", "qty": 2}]})], "Bill ban gaya.")
    r = client.post("/api/app/chat", json={"text": "2 maggi ka bill"}, headers=h).json()
    [f] = r["files"]
    got = client.get(f"/api/app/file/{f['id']}", headers=h)
    assert got.status_code == 200 and got.content.startswith(b"%PDF")


def test_files_are_private(client, session, monkeypatch):
    h = auth(sign_up(client))
    inv.add_product(session, session.scalar(select(User.id)), "Maggi", price="14", opening_stock="10")
    script(monkeypatch, [("create_bill", {"items": [{"product_name": "Maggi", "qty": 1}]})], "ok")
    fid = client.post("/api/app/chat", json={"text": "bill"}, headers=h).json()["files"][0]["id"]
    other = auth(sign_up(client, phone=PROF_PHONE, role="professor", name="Prof"))
    assert client.get(f"/api/app/file/{fid}", headers=other).status_code == 404


def test_file_outside_bills_dir_is_never_served(client, session):
    h = auth(sign_up(client))
    session.add(Notification(user_id=session.scalar(select(User.id)), file_path="/etc/passwd", filename="x", seen=True))
    session.flush()
    nid = session.scalar(select(Notification.id))
    assert client.get(f"/api/app/file/{nid}", headers=h).status_code == 404


def test_supplier_order_becomes_a_share_card(client, session, monkeypatch):
    h = auth(sign_up(client))
    msg.add_supplier(session, session.scalar(select(User.id)), "Ramesh Traders", "9876543210")
    order = ("order_from_supplier", {"supplier_name": "Ramesh Traders", "items": [{"product_name": "Maggi", "qty": 24}]})
    script(monkeypatch, [order], "Confirm?")
    client.post("/api/app/chat", json={"text": "Ramesh se 24 maggi"}, headers=h)
    r = client.post("/api/app/chat", json={"text": "haan"}, headers=h).json()
    [card] = r["shares"]
    assert card["to"] == "919876543210" and "Maggi: 24" in card["text"]


def test_reminder_share_card_carries_the_qr(client, session, monkeypatch):
    h = auth(sign_up(client))
    owner = session.scalar(select(User))
    owner.upi_id = "yash@okaxis"
    shop.add_credit(session, owner.id, "Sharma", 300)
    msg.set_customer_phone(session, owner.id, "Sharma", "9811122233")
    script(monkeypatch, [("send_payment_reminder", {"customer_name": "Sharma"})], "Bhejun?")
    client.post("/api/app/chat", json={"text": "Sharma ko yaad dilao"}, headers=h)
    [card] = client.post("/api/app/chat", json={"text": "haan"}, headers=h).json()["shares"]
    assert card["to"] == "919811122233" and "upi://pay" in card["text"]
    assert client.get(f"/api/app/file/{card['file_id']}", headers=h).content.startswith(b"\x89PNG")


def test_class_message_reaches_students_inbox(client, session, monkeypatch):
    p = auth(sign_up(client, phone=PROF_PHONE, role="professor", name="Prof Rao"))
    code, _ = add_ravi(session)
    s = auth(sign_up(client, phone="9822200001", role="student", setup_code=code, pin="222222"))
    accept_invites(session)
    script(monkeypatch, [("remind_class", {"class_name": "CSE-3", "message": "Kal lab hai"})], "Bhejun?")
    client.post("/api/app/chat", json={"text": "class ko bolo kal lab"}, headers=p)
    assert client.post("/api/app/chat", json={"text": "haan"}, headers=p).json()["shares"] == []
    texts = [i["text"] for i in client.get("/api/app/inbox", headers=s).json()["items"]]
    assert "Prof Rao (CSE-3): Kal lab hai" in texts


def test_failed_turn_returns_nothing_extra(client, session, monkeypatch):
    h = auth(sign_up(client))
    session.commit()  # in the real app the sign-up request committed long before this turn
    script(monkeypatch, [("add_credit", {"customer_name": "X", "amount": "5"})])  # script runs out -> fails
    r = client.post("/api/app/chat", json={"text": "x"}, headers=h).json()
    assert r["reply"] == graph.FAILED and r["files"] == r["shares"] == r["messages"] == []


@pytest.mark.parametrize("text", ["", "   ", "x" * 2001])
def test_chat_length_checked(client, text):
    h = auth(sign_up(client))
    assert client.post("/api/app/chat", json={"text": text}, headers=h).status_code == 400


# --- voice ------------------------------------------------------------------------


def test_voice_note(client, monkeypatch):
    h = auth(sign_up(client))
    monkeypatch.setattr(app_api, "transcribe", lambda data, mime: "Gupta ka balance batao")
    script(monkeypatch, "Gupta ka koi khata nahi hai.")
    r = client.post("/api/app/voice", files={"audio": ("v.aac", b"\x00\x01", "audio/aac")}, headers=h).json()
    assert r["heard"] == "Gupta ka balance batao" and r["reply"] == "Gupta ka koi khata nahi hai."


def test_voice_refusals(client, monkeypatch):
    h = auth(sign_up(client))
    assert client.post("/api/app/voice", files={"audio": ("v.exe", b"x", "application/octet-stream")},
                       headers=h).status_code == 415

    def deaf(data, mime):
        raise app_api.VoiceError("no speech")

    monkeypatch.setattr(app_api, "transcribe", deaf)
    r = client.post("/api/app/voice", files={"audio": ("v.aac", b"x", "audio/aac")}, headers=h)
    assert r.status_code == 422 and "type it" in r.json()["detail"]
