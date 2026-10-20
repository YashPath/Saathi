"""P4 checks: dashboard links, API access rules and data, the page."""

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.agent import outbox
from app.agent.roles import billing, dashboard_link
from app.agent.roles import inventory as inv
from app.agent.roles import professor as prof
from app.agent.roles import shopkeeper as shop
from app.config import get_settings
from app.dashboard import api, auth
from app.db.models import ActionLog, Message, User
from tests.conftest import accept_invites, needs_db

SECRET = "test-dashboard-secret-0123456789abcdef"  # 32+ chars


@pytest.fixture(autouse=True)
def configured(monkeypatch, tmp_path):
    monkeypatch.setattr(get_settings(), "dashboard_secret", SECRET)
    monkeypatch.setattr(get_settings(), "public_base_url", "https://demo.example/")
    monkeypatch.setattr(get_settings(), "bills_dir", str(tmp_path))


# --- tokens -----------------------------------------------------------------


def test_token_round_trip():
    assert auth.read_token(auth.make_token(7)) == (7, 0)
    assert auth.read_token(auth.make_token(7, version=3)) == (7, 3)


@pytest.mark.parametrize("mangle", [
    lambda t: t[:-1] + ("0" if t[-1] != "0" else "1"),  # signature changed
    lambda t: "8" + t[1:],  # user id changed
    lambda t: "7.1." + t.split(".", 2)[2],  # version changed
    lambda t: "", lambda t: "a.b", lambda t: "x.y.z", lambda t: "a.b.c.d", lambda t: t + ".1",
    lambda t: t[:-1] + "é",  # non-ASCII: must be rejected, not crash (compare_digest raises on str)
    lambda t: "٧" + t[1:],  # Arabic-Indic digit 7: isdigit() is true, int() accepts it
])
def test_tampered_tokens_rejected(mangle):
    assert auth.read_token(mangle(auth.make_token(7))) is None


def test_expired_token_rejected():
    old = auth.make_token(7, now=1_000_000)
    assert auth.read_token(old, now=1_000_000 + 25 * 3600) is None


def test_no_secret_means_no_dashboard(monkeypatch):
    token = auth.make_token(7)
    monkeypatch.setattr(get_settings(), "dashboard_secret", "")
    assert auth.read_token(token) is None
    with pytest.raises(RuntimeError):
        auth.make_token(7)


# --- API ----------------------------------------------------------------------


@pytest.fixture
def client(session):
    app = FastAPI()
    app.include_router(api.router)

    def test_db():
        yield session

    app.dependency_overrides[api.db] = test_db
    return TestClient(app)


def get(client, path, user_id=None, token=None):
    tok = token if token is not None else auth.make_token(user_id)
    return client.get("/api/" + path, headers={"Authorization": f"Bearer {tok}"})


@pytest.fixture
def people(session):
    shopk = User(phone="911", name="Yash Store", role="shopkeeper")
    other = User(phone="912", name="Other Store", role="shopkeeper")
    professor = User(phone="922", name="Prof Rao", role="professor")
    session.add_all([shopk, other, professor])
    session.flush()
    inv.add_product(session, shopk.id, "Maggi", price="14", opening_stock="5", reorder_level="10")
    billing.create_bill(session, shopk.id, [{"product_name": "Maggi", "qty": "2"}], customer_name="Sharma", on_credit=True)
    shop.add_credit(session, other.id, "Secret Customer", 999)
    prof.add_class(session, professor.id, "CSE-3")
    prof.add_students(session, professor.id, "CSE-3", [{"roll_no": 1, "name": "Ravi", "phone": "9811111111"},
                                                       {"roll_no": 2, "name": "Neha", "phone": "9822222222"}])
    prof.mark_attendance(session, professor.id, "CSE-3", [2])
    prof.record_marks(session, professor.id, "CSE-3", "Quiz", "10", [{"roll_no": 1, "score": 8}, {"roll_no": 2, "score": 6}])
    accept_invites(session)
    ravi = session.scalar(select(User).where(User.phone == "919811111111"))
    return {"shop": shopk.id, "other": other.id, "prof": professor.id, "ravi": ravi.id}


@needs_db
class TestApi:
    def test_requires_a_valid_token(self, client, people):
        assert get(client, "me", token="").status_code == 401
        assert get(client, "me", token="1.2.3").status_code == 401
        assert get(client, "me", people["shop"]).json() == {"name": "Yash Store", "role": "shopkeeper"}

    def test_shop_data(self, client, people):
        d = get(client, "shop", people["shop"]).json()
        assert d["today"]["sales"] == 28.0 and d["today"]["credit_sales"] == 28.0
        assert d["dues"] == [{"customer": "Sharma", "balance": 28.0}]
        assert d["low_stock"][0]["name"] == "Maggi" and len(d["week"]) == 7 and d["week"][-1]["sales"] == 28.0
        assert d["bills"][0]["no"] == 1 and d["bills"][0]["customer"] == "Sharma"

    def test_shops_never_see_each_other(self, client, people):
        d = get(client, "shop", people["shop"]).json()
        assert "Secret Customer" not in str(d)
        other = get(client, "shop", people["other"]).json()
        assert other["dues"] == [{"customer": "Secret Customer", "balance": 999.0}] and other["bills"] == []

    def test_logout_kills_old_links(self, client, session, people):
        from app.agent.roles.dashboard_link import dashboard_logout

        old = auth.make_token(people["shop"], 0)
        assert get(client, "me", token=old).status_code == 200
        assert dashboard_logout(session, people["shop"])["ok"]
        assert get(client, "me", token=old).status_code == 401
        new = auth.make_token(people["shop"], 1)
        assert get(client, "me", token=new).status_code == 200
        assert get(client, "me", people["other"]).status_code == 200  # other users unaffected

    def test_role_walls(self, client, people):
        assert get(client, "classes", people["shop"]).status_code == 403
        assert get(client, "shop", people["prof"]).status_code == 403
        assert get(client, "shop", people["ravi"]).status_code == 403
        assert get(client, "classes", people["ravi"]).status_code == 403

    def test_professor_classes(self, client, people):
        [c] = get(client, "classes", people["prof"]).json()["classes"]
        assert c["name"] == "CSE-3" and c["students"] == 2 and c["attendance"] == 50.0
        assert c["low_attendance"] == [{"roll": 2, "name": "Neha", "percent": 0.0}]
        assert c["assessments"] == [{"name": "Quiz", "average_percent": 70.0}]

    def test_student_sees_only_self(self, client, people):
        [c] = get(client, "student", people["ravi"]).json()["classes"]
        assert c == {"class": "CSE-3", "roll": 1, "present": 1, "days": 1, "attendance": 100.0,
                     "marks": [{"name": "Quiz", "score": 8.0, "out_of": 10.0}]}
        assert "Neha" not in str(get(client, "student", people["ravi"]).json())

    def test_activity_is_per_user(self, client, people):
        acts = get(client, "activity", people["shop"]).json()["actions"]
        tools = {a["tool"] for a in acts}
        assert "create_bill" in tools and "add_class" not in tools

    def test_dashboard_link_never_exposes_the_token(self, session, people):
        outbox.take(session)  # the fixture's bill PDF
        r = dashboard_link.dashboard_link(session, people["shop"])
        assert r["ok"] and "#t=" not in r["message"]
        [item] = outbox.take(session)
        assert item["to"] == "911" and item["text"].split("\n")[1].startswith("https://demo.example/dashboard#t=")
        token = item["text"].split("#t=")[1]
        assert auth.read_token(token) == (people["shop"], 0)
        logged = str(session.scalars(select(ActionLog.output_json)).all()) + str(session.scalars(select(Message.content)).all())
        assert token not in logged


def test_dashboard_link_without_secret(monkeypatch):
    monkeypatch.setattr(get_settings(), "dashboard_secret", "")
    assert not dashboard_link.dashboard_link(None, 1)["ok"]


def test_page_has_no_inline_token_and_loads_libraries():
    page = (Path(__file__).parent.parent / "app" / "static" / "dashboard.html").read_text()
    assert "history.replaceState" in page and "Authorization" in page
    # exact files that exist in these package versions (chart.js 4.4.1 ships chart.umd.js, not chart.umd.min.js)
    for lib in ("react@18.3.1/umd/react.production.min.js", "react-dom@18.3.1/umd/react-dom.production.min.js",
                "htm@3.1.1/dist/htm.umd.js", "chart.js@4.4.1/dist/chart.umd.js"):
        assert f"https://cdn.jsdelivr.net/npm/{lib}" in page


def test_app_serves_page_and_api():
    from app.main import app

    c = TestClient(app)
    page = c.get("/dashboard")
    assert page.status_code == 200 and "Saathi" in page.text and page.headers["referrer-policy"] == "no-referrer"
    assert c.get("/api/me").status_code == 401


def test_every_role_can_ask_for_the_dashboard():
    from app.agent.roles.registry import ROLES

    assert all("dashboard_link" in {t.__name__ for t in r.tools} for r in ROLES.values())


def test_short_secret_keeps_the_dashboard_off(monkeypatch):
    monkeypatch.setattr(get_settings(), "dashboard_secret", "short")
    assert auth.read_token("1.0.9999999999.abc") is None
    with pytest.raises(RuntimeError):
        auth.make_token(1)
    assert not dashboard_link.dashboard_link(None, 1)["ok"]


def test_page_security_headers_and_sri():
    from app.main import app

    page = TestClient(app).get("/dashboard")
    assert page.headers["x-frame-options"] == "DENY" and "frame-ancestors 'none'" in page.headers["content-security-policy"]
    assert page.text.count('integrity="sha384-') == 4 and page.text.count('crossorigin="anonymous"') == 4
