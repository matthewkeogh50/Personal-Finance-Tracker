"""Pulling GBP expenses into one CHF "UK expenses" line, and the daily exchange-rate job."""
from datetime import date

from fastapi.testclient import TestClient

from app import fx
from app.main import app

M = "2027-03"


def total(c):
    return c.get(f"/api/reports/monthly?month={M}").json()["expenses"]["total_chf"]


def test_pull_uk_expenses():
    today = date.today().isoformat()
    with TestClient(app) as c:
        c.put("/api/fx", json={"currency": "GBP", "date": today, "rate": 1.2})
        assert c.post("/api/transactions/rollup", json={"month": M}).status_code == 422        # nothing to pull
        for amount in (100, 50):
            c.post("/api/transactions", json={"date": f"{M}-10", "description": f"UK {amount}", "amount": amount,
                                              "currency": "GBP", "fx_rate": 1.1, "category": "Food"})
        c.post("/api/transactions", json={"date": f"{M}-11", "description": "Migros", "amount": 200, "category": "Food"})
        assert total(c) == 200 + 165

        pre = c.get(f"/api/transactions/rollup?month={M}").json()
        assert pre["total"] == 150 and pre["rate"] == 1.2 and pre["amount_chf"] == 198 and pre["existing"] is None

        line = c.post("/api/transactions/rollup", json={"month": M})
        assert line.status_code == 201
        line = line.json()
        assert line["description"] == "UK expenses" and line["currency"] == "CHF" and line["date"] == f"{M}-31"
        assert line["amount_chf"] == 198 and line["category"] == "UK expenses" and line["is_rollup"] == 1
        assert total(c) == 200 + 198                                   # the line replaces the GBP rows, no double count
        rep = c.get(f"/api/reports/monthly?month={M}").json()
        assert rep["expenses"]["foreign_currency"][0]["total"] == 150  # still visible as GBP spending
        assert rep["expenses"]["transactions"] == 2
        food = next(b for b in rep["expenses"]["by_category"] if b["category"] == "Food")
        assert food["spent_chf"] == 200
        assert c.get(f"/api/reports/yearly?year=2027").json()["expenses_chf"] == 398
        assert c.get(f"/api/reports/categories?months=1&end={M}").json()["categories"][0]["total_chf"] == 200

        # a later GBP expense counts on its own until the next pull, which replaces the line
        c.post("/api/transactions", json={"date": f"{M}-20", "description": "UK late", "amount": 10,
                                          "currency": "GBP", "fx_rate": 1.1})
        assert total(c) == 200 + 198 + 11
        assert c.get(f"/api/transactions/rollup?month={M}").json()["not_yet_pulled"] == 1
        again = c.post("/api/transactions/rollup", json={"month": M, "fx_rate": 1.25, "fee_pct": 0}).json()
        assert again["amount_chf"] == 200 and again["pulled"]["replaced_existing_line"]
        lines = [t for t in c.get(f"/api/transactions?month={M}").json() if t["is_rollup"]]
        assert len(lines) == 1 and total(c) == 400

        # deleting the line puts the individual GBP expenses back into the totals
        assert c.delete(f"/api/transactions/{again['id']}").status_code == 200
        assert total(c) == 200 + 165 + 11
        assert not any(t["rollup_id"] for t in c.get(f"/api/transactions?month={M}").json())


def test_daily_rates(monkeypatch):
    today = date.today().isoformat()
    with TestClient(app) as c:
        c.put("/api/fx", json={"currency": "GBP", "date": today, "rate": 1.2})                  # set by hand
        c.post("/api/transactions", json={"date": "2027-04-01", "description": "US", "amount": 1,
                                          "currency": "USD", "fx_rate": 0.9})

        def rate_today(currency):
            return [r for r in c.get(f"/api/fx?currency={currency}").json() if r["date"] == today]

        monkeypatch.setattr(fx, "_fetch_rate", lambda cur, d: 0.88)
        assert fx.refresh_daily_rates()["rates"] == {"USD": 0.88}
        monkeypatch.setattr(fx, "_fetch_rate", lambda cur, d: 0.89)
        fx.refresh_daily_rates()
        assert rate_today("USD")[0]["rate"] == 0.89 and rate_today("USD")[0]["source"] == "auto"
        assert rate_today("GBP")[0]["rate"] == 1.2 and rate_today("GBP")[0]["source"] == "manual"
        monkeypatch.setattr(fx, "_fetch_rate", lambda cur, d: None)                             # offline
        assert fx.refresh_daily_rates()["rates"] == {}
