"""Money received (UK rent), the "needs to be sent to the UK" figure, and re-applying today's rate."""
from datetime import date

from fastapi.testclient import TestClient

from app import fx
from app.main import app

M = "2027-06"


def uk(c):
    return c.get(f"/api/reports/monthly?month={M}").json()["uk_transfer"]


def test_uk_transfer_and_reapply(monkeypatch):
    today = date.today().isoformat()
    with TestClient(app) as c:
        def add(desc, amount, **kw):
            r = c.post("/api/transactions", json={"date": f"{M}-10", "description": desc, "amount": amount,
                                                  "currency": "GBP", "fx_rate": 1.1, **kw})
            assert r.status_code == 201
            return r.json()

        add("UK mortgage", 900)
        add("UK council tax", 150)
        rent = add("Rent received", -800)                       # money in = negative expense
        assert rent["amount_chf"] == -880
        c.post("/api/transactions", json={"date": f"{M}-11", "description": "Migros", "amount": 100})
        rec = c.post("/api/recurring", json={"name": "UK insurance", "amount": 50, "currency": "GBP",
                                             "start_month": M, "end_month": M}).json()

        u = uk(c)
        assert (u["charges"], u["received"], u["unpaid_recurring"], u["to_send"]) == (1050, 800, 50, 300)
        assert u["unpaid_recurring_names"] == ["UK insurance"]
        rep = c.get(f"/api/reports/monthly?month={M}").json()
        assert rep["expenses"]["total_chf"] == 100 + 275        # rent reduces what was spent

        # paying the recurring charge moves it from "not yet paid" to charges; the total stays
        c.post(f"/api/recurring/{rec['id']}/pay", json={"month": M, "fx_rate": 1.1})
        u = uk(c)
        assert (u["charges"], u["unpaid_recurring"], u["to_send"], u["to_send_chf"]) == (1100, 0, 300, 330)

        # re-apply today's live rate to the month
        monkeypatch.setattr(fx, "AUTO_FETCH", True)
        monkeypatch.setattr(fx, "_fetch_rate", lambda cur, d: 1.2)
        c.put("/api/fx", json={"currency": "GBP", "date": "2020-01-01", "rate": 1.0})   # an old rate, not today's
        other = c.post("/api/transactions", json={"date": "2027-07-01", "description": "next month", "amount": 10,
                                                  "currency": "GBP", "fx_rate": 1.1}).json()
        r = c.post("/api/fx/reapply", json={"month": M})
        assert r.status_code == 200, r.text
        r = r.json()
        if r["source"] == "live":                               # no hand-set rate for today from another test
            assert r["rate"] == 1.2 and r["updated_transactions"] == 4
            assert (r["before_chf"], r["after_chf"]) == (330, 360)
            assert uk(c)["to_send_chf"] == 360
            assert [x for x in c.get("/api/fx?currency=GBP").json() if x["date"] == today][0]["rate"] == 1.2
        assert c.get("/api/transactions?date_from=2027-07-01&date_to=2027-07-01").json()[0]["amount_chf"] == other["amount_chf"]

        # the live path, in a currency no other test has set a rate for by hand
        c.post("/api/transactions", json={"date": "2027-08-02", "description": "Paris", "amount": 100,
                                          "currency": "EUR", "fx_rate": 0.9})
        monkeypatch.setattr(fx, "_fetch_rate", lambda cur, d: 0.95)
        r = c.post("/api/fx/reapply", json={"currency": "EUR", "month": "2027-08"}).json()
        assert (r["source"], r["rate"], r["before_chf"], r["after_chf"]) == ("live", 0.95, 90, 95)
        assert [x for x in c.get("/api/fx?currency=EUR").json() if x["date"] == today][0]["source"] == "auto"
        monkeypatch.setattr(fx, "_fetch_rate", lambda cur, d: None)             # offline -> latest stored rate
        r = c.post("/api/fx/reapply", json={"currency": "EUR", "month": "2027-08"}).json()
        assert r["rate"] == 0.95 and "stored" in r["source"]

        # an explicit rate always wins, and an existing "UK expenses" line follows it, keeping its fee
        line = c.post("/api/transactions/rollup", json={"month": M, "fx_rate": 1.2, "fee_pct": 5}).json()
        assert line["amount_chf"] == round(300 * 1.2 * 1.05, 2)
        r = c.post("/api/fx/reapply", json={"month": M, "fx_rate": 1.3}).json()
        assert r["source"] == "given" and r["summary_line"]["after_chf"] == round(300 * 1.3 * 1.05, 2)
        assert c.get(f"/api/reports/monthly?month={M}").json()["expenses"]["total_chf"] == 100 + 409.5
        assert uk(c)["to_send_chf"] == 390

        # more rent than charges: no transfer fee is added to a surplus
        add("Deposit returned", -1000)
        pre = c.get(f"/api/transactions/rollup?month={M}").json()
        assert pre["total"] == -700 and pre["amount_chf"] == round(-700 * pre["rate"], 2)
        assert uk(c)["to_send"] == -700
        assert c.post("/api/fx/reapply", json={"currency": "CHF"}).status_code == 422
