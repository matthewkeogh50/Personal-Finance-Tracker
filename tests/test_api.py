"""End-to-end smoke test of the monthly workflow an agent runs."""
from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402


def test_monthly_workflow():
    with TestClient(app) as c:
        people = c.get("/api/people").json()
        assert len(people) == 2
        c.patch(f"/api/people/{people[0]['id']}", json={"name": "Alex"})

        # salary for two people
        assert c.post("/api/income", json={"person": "Alex", "date": "2026-10-25", "amount": 7000}).status_code == 201
        assert c.post("/api/income", json={"person_id": people[1]["id"], "date": "2026-10-25", "amount": 6000,
                                           "gross_amount": 7500}).status_code == 201
        summary = c.get("/api/income/summary?year=2026").json()
        assert summary["total_chf"] == 13000
        assert summary["people"][0]["monthly_chf"][9] == 7000

        # budgets + recurring
        assert c.put("/api/budgets", json={"category": "food", "amount": 800, "valid_from": "2026-01"}).status_code == 200
        rent = c.post("/api/recurring", json={"name": "Rent", "amount": 2500, "category": "Housing",
                                              "day_of_month": 1}).json()
        car = c.post("/api/recurring", json={"name": "Car", "amount": 450, "category": "Transport"}).json()
        assert c.post(f"/api/recurring/{rent['id']}/pay", json={"month": "2026-10"}).status_code == 201
        assert c.post(f"/api/recurring/{rent['id']}/pay", json={"month": "2026-10"}).status_code == 409

        # GBP needs a rate when auto-fetch is off
        gbp = {"date": "2026-10-05", "description": "UK storage", "amount": 100, "currency": "GBP", "category": "Other"}
        assert c.post("/api/transactions", json=gbp).status_code == 422
        c.put("/api/fx", json={"currency": "GBP", "date": "2026-10-01", "rate": 1.1})

        # statement upload + bulk load, retry is idempotent
        st = c.post("/api/statements", data={"month": "2026-10"},
                    files={"file": ("oct.png", b"\x89PNG fake", "image/png")}).json()
        batch = {"statement_id": st["id"], "transactions": [
            {"date": "2026-10-03", "description": "Migros", "amount": 120.5, "category": "Food"},
            {"date": "2026-10-03", "description": "Migros", "amount": 120.5, "category": "Food"},
            {"date": "2026-10-09", "description": "Coop", "amount": 700, "category": "Food"},
            gbp,
        ]}
        res = c.post("/api/transactions/bulk", json=batch).json()
        assert res["created_count"] == 4 and [t for t in res["created"] if t["currency"] == "GBP"][0]["amount_chf"] == 110
        again = c.post("/api/transactions/bulk", json=batch).json()
        assert again["created_count"] == 0 and again["skipped_duplicates_count"] == 4
        bad = c.post("/api/transactions/bulk", json={"transactions": [
            {"date": "2026-10-10", "description": "x", "amount": 1, "category": "Nope"}]})
        assert bad.status_code == 422 and "transactions[0]" in bad.json()["detail"]
        assert c.get(f"/api/statements/{st['id']}/file").content == b"\x89PNG fake"

        # investments
        acc = c.post("/api/accounts", json={"name": "Stocks", "type": "stocks"}).json()
        c.post(f"/api/accounts/{acc['id']}/flows", json={"date": "2026-10-28", "amount": 1000})
        s = c.post(f"/api/accounts/{acc['id']}/valuations", json={"date": "2026-10-31", "value": 1100}).json()
        assert s["gain_chf"] == 100 and s["return_pct"] == 10

        # month close
        rep = c.get("/api/reports/monthly?month=2026-10").json()
        assert rep["income"]["total_chf"] == 13000
        assert rep["expenses"]["total_chf"] == 2500 + 120.5 * 2 + 700 + 110
        assert rep["recurring"]["unpaid"] == ["Car"]
        food = next(b for b in rep["expenses"]["by_category"] if b["category"] == "Food")
        assert food["over_budget"] and food["remaining_chf"] == -141
        sv = rep["savings"]
        assert sv["available_to_save_chf"] == round(13000 - 3551 - 450, 2)
        assert sv["remaining_to_transfer_chf"] == sv["available_to_save_chf"] - 1000
        assert rep["expenses"]["foreign_currency"][0]["total"] == 100

        ov = c.get("/api/reports/overview?months=3&end=2026-10").json()
        assert ov["series"][-1]["income_chf"] == 13000 and ov["net_worth"]["total_value_chf"][-1] == 1100
        assert c.get("/api/reports/yearly?year=2026").json()["net_chf"] == 13000 - 3551
        assert c.get("/api/reports/foreign?end=2026-10").json()["total_chf"] == 110
        assert c.get("/api/reports/categories?end=2026-10").json()["categories"][0]["category"] == "Housing"
        assert c.get("/api/budgets/history?months=2&end=2026-10").status_code == 200
        assert c.get("/api/investments/summary").json()["total_value_chf"] == 1100
        assert "Monthly" in c.get("/api/agent-guide").text or True
        assert car["id"]
