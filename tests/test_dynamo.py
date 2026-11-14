"""DynamoDB-specific behaviour: data survives a restart, cascades are persisted, failures roll back."""
import os

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.skipif(os.environ.get("TEST_STORAGE") != "dynamodb", reason="needs TEST_STORAGE=dynamodb")


def snapshot(c):
    paths = ["/api/people", "/api/categories", "/api/budgets", "/api/income", "/api/statements", "/api/recurring",
             "/api/transactions", "/api/fx?currency=GBP", "/api/accounts", "/api/reports/monthly?month=2026-10"]
    return {p: c.get(p).json() for p in paths}


def test_persistence_cascades_and_failure():
    from app import db
    from app.dynamo import StoreError
    from app.main import app

    import boto3
    ddb = boto3.client("dynamodb", region_name=os.environ["AWS_REGION"])
    if os.environ["DYNAMODB_TABLE"] in ddb.list_tables()["TableNames"]:      # start from an empty table
        ddb.delete_table(TableName=os.environ["DYNAMODB_TABLE"])

    with TestClient(app) as c:
        assert c.get("/api/health").json()["storage"] == "dynamodb"
        c.put("/api/fx", json={"currency": "GBP", "date": "2026-10-01", "rate": 1.1})
        c.put("/api/budgets", json={"category": "Food", "amount": 800.5, "valid_from": "2026-01"})
        c.post("/api/income", json={"person_id": 1, "date": "2026-10-25", "amount": 7000.25})
        big = bytes(range(256)) * 3000                                   # 768 KB -> 3 chunks
        st = c.post("/api/statements", data={"month": "2026-10"}, files={"file": ("s.png", big, "image/png")}).json()
        txs = [{"date": "2026-10-%02d" % (i % 28 + 1), "description": f"shop {i}", "amount": 1.5 + i, "category": "Food"}
               for i in range(230)]                                      # > 100 -> several write groups
        txs.append({"date": "2026-10-05", "description": "UK", "amount": 10, "currency": "GBP"})
        assert c.post("/api/transactions/bulk", json={"transactions": txs, "statement_id": st["id"]}).json()["created_count"] == 231
        acc = c.post("/api/accounts", json={"name": "Stocks", "type": "stocks"}).json()
        c.post(f"/api/accounts/{acc['id']}/flows", json={"date": "2026-10-01", "amount": 1000})
        c.post(f"/api/accounts/{acc['id']}/valuations", json={"date": "2026-10-31", "value": 1100})
        gone = c.post("/api/accounts", json={"name": "Old", "type": "cash"}).json()
        c.post(f"/api/accounts/{gone['id']}/valuations", json={"date": "2026-10-31", "value": 5})
        assert c.delete(f"/api/accounts/{gone['id']}").status_code == 200           # cascade delete
        other = next(x for x in c.get("/api/categories").json() if x["name"] == "Other")
        c.patch("/api/transactions/1", json={"category_id": other["id"]})
        assert c.delete(f"/api/categories/{other['id']}").status_code == 200       # cascade set-null
        tx = c.get("/api/transactions?q=shop 0").json()
        c.delete(f"/api/transactions/{tx[-1]['id']}")
        before = snapshot(c)

        # "restart": throw the mirror away and rebuild it purely from DynamoDB
        assert c.post("/api/admin/reload").json()["rows"]["transactions"] == 230
        assert snapshot(c) == before
        assert db._store.load("valuations") and len(db._store.load("valuations")) == 1
        assert c.get(f"/api/statements/{st['id']}/file").content == big
        c.delete(f"/api/statements/{st['id']}")
        assert db._store.get_file(st["stored_name"]) is None

        # a failed DynamoDB write fails the request and leaves no trace
        real = db._store.apply

        def boom(*a, **k):
            raise StoreError("simulated outage")

        db._store.apply = boom
        r = c.post("/api/income", json={"person_id": 1, "date": "2026-10-26", "amount": 1})
        db._store.apply = real
        assert r.status_code == 503
        assert len(c.get("/api/income").json()) == 1
        assert c.post("/api/income", json={"person_id": 2, "date": "2026-10-26", "amount": 2}).status_code == 201

    # a brand new process start sees the same data
    with TestClient(app) as c2:
        assert len(c2.get("/api/income").json()) == 2 and len(c2.get("/api/people").json()) == 2
