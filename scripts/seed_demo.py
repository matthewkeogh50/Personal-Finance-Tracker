"""Fill an EMPTY instance with 14 months of made-up data so the dashboards can be previewed.

    python scripts/seed_demo.py [http://localhost:1311]

Only for trying the app out: it refuses to run if any income or transactions already exist.
"""
import json
import random
import sys
import urllib.request
from datetime import date

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:1311").rstrip("/") + "/api"
random.seed(7)


def call(method, path, body=None):
    req = urllib.request.Request(BASE + path, method=method, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req) as r:
        return json.load(r)


def months_back(n):
    y, m = date.today().year, date.today().month
    out = []
    for _ in range(n):
        out.append((y, m))
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    return out[::-1]


if call("GET", "/income") or call("GET", "/transactions?limit=1"):
    sys.exit("This instance already has data; not seeding.")

p1, p2 = call("GET", "/people")[:2]
for cat, amt in [("Food", 1100), ("Travel", 600), ("Entertainment", 300), ("Shopping", 400), ("Transport", 250)]:
    call("PUT", "/budgets", {"category": cat, "amount": amt, "valid_from": "2020-01"})
rec = [call("POST", "/recurring", r) for r in [
    {"name": "Rent", "amount": 2650, "category": "Housing", "day_of_month": 1},
    {"name": "Car lease", "amount": 489, "category": "Transport", "day_of_month": 5},
    {"name": "Health insurance", "amount": 780, "category": "Insurance", "day_of_month": 3},
    {"name": "Internet + mobile", "amount": 129, "category": "Utilities", "day_of_month": 12},
    {"name": "UK storage unit", "amount": 85, "currency": "GBP", "category": "Other", "day_of_month": 15},
]]
cash = call("POST", "/accounts", {"name": "Emergency cash", "type": "savings", "institution": "ZKB"})
stocks = call("POST", "/accounts", {"name": "Stocks", "type": "stocks", "institution": "Interactive Brokers"})
pillar = call("POST", "/accounts", {"name": "Pillar 3a", "type": "pension", "institution": "VIAC"})
isa = call("POST", "/accounts", {"name": "UK ISA", "type": "stocks", "currency": "GBP", "institution": "Vanguard UK"})

shops = {"Food": ["Migros", "Coop", "Denner", "Restaurant", "Bakery"], "Travel": ["SBB", "Swiss", "Hotel", "Airbnb"],
         "Entertainment": ["Cinema", "Concert", "Spotify"], "Shopping": ["Galaxus", "Zara", "IKEA"],
         "Transport": ["Fuel", "Parking"], "Health": ["Pharmacy", "Dentist"]}
mean = {"Food": 55, "Travel": 140, "Entertainment": 40, "Shopping": 90, "Transport": 45, "Health": 70}
count = {"Food": 18, "Travel": 4, "Entertainment": 6, "Shopping": 4, "Transport": 5, "Health": 2}

v_cash, v_stocks, v_pillar, v_isa, paid_stocks = 18000.0, 30000.0, 21000.0, 9500.0, 30000.0
call("POST", f"/accounts/{stocks['id']}/flows", {"date": f"{months_back(15)[0][0]}-{months_back(15)[0][1]:02d}-01", "amount": 30000, "note": "Opening balance"})
call("POST", f"/accounts/{cash['id']}/flows", {"date": f"{months_back(15)[0][0]}-{months_back(15)[0][1]:02d}-01", "amount": 18000, "note": "Opening balance"})
call("POST", f"/accounts/{pillar['id']}/flows", {"date": f"{months_back(15)[0][0]}-{months_back(15)[0][1]:02d}-01", "amount": 21000, "note": "Opening balance"})

today = date.today()
for i, (y, m) in enumerate(months_back(14)):
    ym = f"{y}-{m:02d}"
    current = (y, m) == (today.year, today.month)
    last_day = today.day if current else 28
    rate = round(1.09 + 0.03 * random.random() + i * 0.002, 4)
    call("PUT", "/fx", {"currency": "GBP", "date": f"{ym}-01", "rate": rate})
    if not current or today.day >= 25:
        call("POST", "/income", {"person_id": p1["id"], "date": f"{ym}-25", "amount": 7450 if i > 5 else 7200, "gross_amount": 9100 if i > 5 else 8800})
        call("POST", "/income", {"person_id": p2["id"], "date": f"{ym}-25", "amount": 6300, "gross_amount": 7700})
        if m == 12:
            call("POST", "/income", {"person_id": p1["id"], "date": f"{ym}-20", "amount": 7100, "kind": "13th_salary"})
            call("POST", "/income", {"person_id": p2["id"], "date": f"{ym}-20", "amount": 6000, "kind": "13th_salary"})
    for r in rec:
        if r["day_of_month"] <= last_day and not (current and r["name"] == "Internet + mobile"):
            call("POST", f"/recurring/{r['id']}/pay", {"month": ym})
    txs = []
    for cat, names in shops.items():
        for _ in range(max(1, round(count[cat] * random.uniform(0.6, 1.3) * (last_day / 28)))):
            txs.append({"date": f"{ym}-{random.randint(1, last_day):02d}", "description": random.choice(names),
                        "amount": round(random.gauss(mean[cat], mean[cat] * 0.45) + 5, 2) if cat != "Travel" else round(random.uniform(20, 380), 2),
                        "category": cat})
    for name in ["Amazon UK", "TV licence"][: random.randint(0, 2)]:
        txs.append({"date": f"{ym}-{random.randint(1, last_day):02d}", "description": name, "currency": "GBP",
                    "amount": round(random.uniform(12, 90), 2), "category": "Shopping"})
    txs = [t for t in txs if t["amount"] > 0]
    call("POST", "/transactions/bulk", {"transactions": txs, "skip_duplicates": False})
    if current:
        continue
    v_cash += 800
    v_stocks = (v_stocks + 1500) * (1 + random.gauss(0.007, 0.028))
    v_pillar = (v_pillar + 588) * (1 + random.gauss(0.004, 0.015))
    v_isa *= 1 + random.gauss(0.005, 0.025)
    call("POST", f"/accounts/{cash['id']}/flows", {"date": f"{ym}-27", "amount": 800})
    call("POST", f"/accounts/{stocks['id']}/flows", {"date": f"{ym}-27", "amount": 1500})
    call("POST", f"/accounts/{pillar['id']}/flows", {"date": f"{ym}-27", "amount": 588})
    for acc, v in [(cash, v_cash), (stocks, v_stocks), (pillar, v_pillar), (isa, v_isa)]:
        call("POST", f"/accounts/{acc['id']}/valuations", {"date": f"{ym}-28", "value": round(v, 2)})

print("Demo data loaded.")
