"""A QR invoice (payment slip) attached to a recurring charge."""
from fastapi.testclient import TestClient

from app import db
from app.main import app


def test_qr_invoice_on_recurring_charge():
    with TestClient(app) as c:
        rec = c.post("/api/recurring", json={"name": "Health insurance QR", "amount": 780, "start_month": "2028-01",
                                             "end_month": "2028-01"}).json()
        url = f"/api/recurring/{rec['id']}/invoice"
        assert rec["invoice_file"] is None and c.get(url).status_code == 404
        assert c.put(url, files={"file": ("slip.txt", b"x", "text/plain")}).status_code == 415

        first = bytes(range(256)) * 2000                                  # 512 KB: several chunks in DynamoDB
        r = c.put(url, files={"file": ("qr-rechnung.png", first, "image/png")})
        assert r.status_code == 200 and r.json()["invoice_filename"] == "qr-rechnung.png"
        got = c.get(url)
        assert got.content == first and got.headers["content-type"] == "image/png"
        items = c.get("/api/recurring/status?month=2028-01").json()["items"]
        item = next(i for i in items if i["recurring_id"] == rec["id"])       # other tests leave open-ended charges
        assert item["has_invoice"] and item["invoice_content_type"] == "image/png"

        # uploading again replaces it and removes the old file
        old_name = r.json()["invoice_file"]
        r = c.put(url, files={"file": ("slip.pdf", b"%PDF-1.4 new", "application/pdf")}).json()
        assert c.get(url).content == b"%PDF-1.4 new" and db.read_file(old_name) is None

        assert c.delete(url).status_code == 200
        assert c.get(url).status_code == 404 and db.read_file(r["invoice_file"]) is None
        assert c.delete(url).status_code == 404

        # deleting the charge removes its file too
        name = c.put(url, files={"file": ("a.png", b"png", "image/png")}).json()["invoice_file"]
        assert c.delete(f"/api/recurring/{rec['id']}").status_code == 200 and db.read_file(name) is None
