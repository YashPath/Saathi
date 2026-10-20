"""P5 checks: the evaluation harness scores correctly (scripted model, no network)."""

import json
from pathlib import Path

import pytest
from sqlalchemy import create_engine

from eval import run_eval
from tests.conftest import TEST_URL, fake_llm, needs_db

CASES = {json.loads(l)["id"]: json.loads(l) for l in (Path(run_eval.__file__).parent / "cases.jsonl").read_text().splitlines() if l.strip()}


def test_cases_file_is_well_formed():
    assert len(CASES) >= 30
    for c in CASES.values():
        assert c["role"] in run_eval.PHONES and c["lang"] in {"en", "hi", "hinglish"} and c["messages"]
        assert all(len(e) == 2 and isinstance(e[1], dict) for e in c["expect"])


@pytest.mark.parametrize("expected,actual,ok", [("500", "500.00", True), ("sharma", "Sharma ji", True),
                                                ("500", "50", False), ("gupta", "Sharma", False), ("true", True, True)])
def test_arg_matching(expected, actual, ok):
    assert run_eval.arg_matches(expected, actual) is ok


def test_score_rules():
    case = {"expect": [["add_credit", {"customer_name": "sharma", "amount": "500"}], ["get_balance", {"customer_name": "gupta"}]]}
    calls = [("add_credit", {"customer_name": "Sharma ji", "amount": "500.00"}, "done"), ("get_balance", {"customer_name": "Gupta"}, "done")]
    assert run_eval.score(case, calls) == (True, True, "ok")
    assert run_eval.score(case, calls[1:])[:2] == (False, False)
    forbid = {"expect": [], "forbid": ["record_payment"]}
    assert run_eval.score(forbid, [("record_payment", {}, "approval")])[0] is True  # parked, then refused: fine
    assert run_eval.score(forbid, [("record_payment", {}, "done")])[0] is False


SCRIPTS = {
    "L06": lambda: fake_llm([("add_credit", {"customer_name": "Sharma ji", "amount": "500"}),
                             ("get_balance", {"customer_name": "Gupta"})], "Done."),
    "L08": lambda: fake_llm([("record_payment", {"customer_name": "Sharma", "amount": "7000"})], "Confirm karein?"),
    "M01": lambda: fake_llm([("order_from_supplier", {"supplier_name": "Ramesh Traders",
                                                      "items": [{"product_name": "Maggi", "qty": 24}]})], "Confirm?"),
    "X01": lambda: fake_llm([("add_credit", {"customer_name": "Sharma", "amount": "500"})], "Main ye nahi kar sakta."),
    "L04": lambda: fake_llm([("list_dues", {})], "Sabka hisaab."),  # wrong tool on purpose
}


@needs_db
def test_harness_end_to_end(monkeypatch, tmp_path):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "bills_dir", str(tmp_path))
    cases = [CASES[i] for i in SCRIPTS]
    rows = {r["id"]: r for r in run_eval.run(cases, create_engine(TEST_URL), lambda c: SCRIPTS[c["id"]]())}
    assert rows["L06"]["success"] and rows["L06"]["intent"]
    assert rows["L08"]["success"]  # "nahi" stopped the payment
    assert rows["M01"]["success"] and "order_from_supplier" in rows["M01"]["tools"]
    assert rows["X01"]["success"]  # the professor role has no add_credit, so it never ran
    assert not rows["L04"]["success"] and not rows["L04"]["intent"] and "missing ['get_balance']" in rows["L04"]["note"]
    text = run_eval.report(list(rows.values()))
    assert "Task success: 80.0% (4/5)" in text and "| lang: hinglish |" in text and "L04" in text
