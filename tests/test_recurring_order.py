"""Recurring charges are listed CHF first, then GBP, then other currencies."""
from fastapi.testclient import TestClient

from app.main import app

M = "2029-02"


def test_recurring_charges_ordered_by_currency():
    with TestClient(app) as c:
        made = [c.post("/api/recurring", json={"name": name, "amount": 10, "currency": cur, "start_month": M,
                                               "end_month": M, "day_of_month": day}).json()
                for name, cur, day in [("A GBP", "GBP", 1), ("B CHF", "CHF", 20), ("C USD", "USD", 1),
                                       ("D GBP", "GBP", None), ("E CHF", "CHF", 5)]]
        ids = {r["id"] for r in made}

        status = [i["name"] for i in c.get(f"/api/recurring/status?month={M}").json()["items"]
                  if i["recurring_id"] in ids]
        assert status == ["E CHF", "B CHF", "A GBP", "D GBP", "C USD"]        # within a currency: by day

        listed = [r["name"] for r in c.get("/api/recurring").json() if r["id"] in ids]
        assert listed == ["B CHF", "E CHF", "A GBP", "D GBP", "C USD"]        # within a currency: by name
