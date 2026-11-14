"""All REST endpoints. Mounted under /api on both the API port and the dashboard port."""
import calendar
import os
import sqlite3
import uuid
from datetime import date as _date
from pathlib import Path
from typing import Literal, Optional

from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, Query, UploadFile
from urllib.parse import quote

from fastapi.responses import PlainTextResponse, Response
from pydantic import BaseModel, Field

from . import db as dbm
from .fx import BASE, resolve_rate, to_chf

API_KEY = os.environ.get("API_KEY", "")
GUIDE_PATH = Path(__file__).resolve().parent.parent / "docs" / "AGENT_GUIDE.md"

DATE = r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"
MONTH = r"^\d{4}-(0[1-9]|1[0-2])$"
CUR = r"^[A-Z]{3}$"
DB = Depends(dbm.get_db)
DEFAULT_TRANSFER_FEE_PCT = 10.0     # added when foreign expenses are pulled into one CHF line (Wise fees)


def require_key(x_api_key: Optional[str] = Header(None), api_key: Optional[str] = Query(None)):
    if API_KEY and API_KEY not in (x_api_key, api_key):
        raise HTTPException(401, "Missing or invalid API key (send it in the X-API-Key header)")


router = APIRouter(prefix="/api", dependencies=[Depends(require_key)])
public = APIRouter(prefix="/api")


# ---------------------------------------------------------------- helpers

def rows(cur) -> list[dict]:
    return [dict(r) for r in cur.fetchall()]


def one(db, sql, params=(), what="Record"):
    r = db.execute(sql, params).fetchone()
    if not r:
        raise HTTPException(404, f"{what} not found")
    return dict(r)


def this_month() -> str:
    return _date.today().strftime("%Y-%m")


def last_months(end: str, n: int) -> list[str]:
    y, m = int(end[:4]), int(end[5:7])
    out = []
    for _ in range(n):
        out.append(f"{y:04d}-{m:02d}")
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    return out[::-1]


def r2(x) -> float:
    return round(x or 0, 2)


def update_row(db, table: str, row_id: int, fields: dict):
    if fields:
        sets = ", ".join(f"{k}=?" for k in fields)
        db.execute(f"UPDATE {table} SET {sets} WHERE id=?", (*fields.values(), row_id))


def delete_row(db, table: str, row_id: int, what: str):
    if db.execute(f"DELETE FROM {table} WHERE id=?", (row_id,)).rowcount == 0:
        raise HTTPException(404, f"{what} not found")
    return {"deleted": row_id}


def resolve_category(db, category_id: Optional[int], name: Optional[str]) -> Optional[int]:
    if category_id is not None:
        one(db, "SELECT id FROM categories WHERE id=?", (category_id,), "Category")
        return category_id
    if name:
        r = db.execute("SELECT id FROM categories WHERE name=? COLLATE NOCASE", (name.strip(),)).fetchone()
        if not r:
            valid = [c["name"] for c in db.execute("SELECT name FROM categories WHERE archived=0 ORDER BY name")]
            raise HTTPException(422, f"Unknown category '{name}'. Valid categories: {', '.join(valid)}. "
                                     f"Create new ones with POST /api/categories.")
        return r["id"]
    return None


def resolve_person(db, person_id: Optional[int], name: Optional[str], required=False) -> Optional[int]:
    if person_id is not None:
        one(db, "SELECT id FROM people WHERE id=?", (person_id,), "Person")
        return person_id
    if name:
        r = db.execute("SELECT id FROM people WHERE name=? COLLATE NOCASE", (name.strip(),)).fetchone()
        if not r:
            valid = [p["name"] for p in db.execute("SELECT name FROM people ORDER BY id")]
            raise HTTPException(422, f"Unknown person '{name}'. Valid people: {', '.join(valid)}")
        return r["id"]
    if required:
        raise HTTPException(422, "person_id or person (name) is required")
    return None


# ---------------------------------------------------------------- meta

@public.get("/health", tags=["meta"])
def health():
    """Liveness check. Never requires an API key."""
    return {"status": "ok", "base_currency": BASE, "auth_required": bool(API_KEY), **dbm.storage_info()}


@router.post("/admin/reload", tags=["meta"])
def admin_reload():
    """Re-read all data from DynamoDB. Only needed if the table was edited outside this app
    (the app otherwise keeps itself in sync). No-op in local SQLite mode."""
    try:
        return dbm.reload_from_store()
    except Exception as e:
        raise HTTPException(503, f"DynamoDB is unavailable: {e}")


@public.get("/agent-guide", response_class=PlainTextResponse, tags=["meta"])
def agent_guide():
    """The full agent guide (markdown): concepts, monthly workflow and every endpoint."""
    return GUIDE_PATH.read_text(encoding="utf-8") if GUIDE_PATH.exists() else "Guide not found."


# ---------------------------------------------------------------- people

class PersonIn(BaseModel):
    name: str = Field(min_length=1)


class PersonPatch(BaseModel):
    name: Optional[str] = Field(None, min_length=1)
    active: Optional[bool] = None


@router.get("/people", tags=["people"])
def list_people(db=DB):
    """The household members (two by default). Rename them with PATCH."""
    return rows(db.execute("SELECT * FROM people ORDER BY id"))


@router.post("/people", status_code=201, tags=["people"])
def create_person(body: PersonIn, db=DB):
    try:
        cur = db.execute("INSERT INTO people (name) VALUES (?)", (body.name.strip(),))
    except sqlite3.IntegrityError:
        raise HTTPException(409, "A person with that name already exists")
    return one(db, "SELECT * FROM people WHERE id=?", (cur.lastrowid,))


@router.patch("/people/{person_id}", tags=["people"])
def update_person(person_id: int, body: PersonPatch, db=DB):
    one(db, "SELECT id FROM people WHERE id=?", (person_id,), "Person")
    try:
        update_row(db, "people", person_id, body.model_dump(exclude_unset=True))
    except sqlite3.IntegrityError:
        raise HTTPException(409, "A person with that name already exists")
    return one(db, "SELECT * FROM people WHERE id=?", (person_id,))


# ---------------------------------------------------------------- income / salary

INCOME_KINDS = Literal["salary", "bonus", "13th_salary", "other"]


class IncomeIn(BaseModel):
    person_id: Optional[int] = None
    person: Optional[str] = Field(None, description="Person name, alternative to person_id")
    date: str = Field(pattern=DATE, description="Pay date YYYY-MM-DD")
    amount: float = Field(description="Net amount paid out, in `currency`")
    currency: str = Field("CHF", pattern=CUR)
    fx_rate: Optional[float] = Field(None, description="CHF per 1 unit of currency; omit to use stored/auto rate")
    gross_amount: Optional[float] = None
    kind: INCOME_KINDS = "salary"
    note: Optional[str] = None


class IncomePatch(BaseModel):
    person_id: Optional[int] = None
    date: Optional[str] = Field(None, pattern=DATE)
    amount: Optional[float] = None
    currency: Optional[str] = Field(None, pattern=CUR)
    fx_rate: Optional[float] = None
    gross_amount: Optional[float] = None
    kind: Optional[INCOME_KINDS] = None
    note: Optional[str] = None


INCOME_SELECT = "SELECT i.*, p.name AS person FROM income i JOIN people p ON p.id=i.person_id"


@router.get("/income", tags=["income"])
def list_income(year: Optional[int] = None, month: Optional[str] = Query(None, pattern=MONTH),
                person_id: Optional[int] = None, db=DB):
    """Income entries (salary payments, bonuses), newest first. Filter by year, month and/or person."""
    where, params = ["1=1"], []
    if year:
        where.append("substr(i.date,1,4)=?"); params.append(str(year))
    if month:
        where.append("substr(i.date,1,7)=?"); params.append(month)
    if person_id:
        where.append("i.person_id=?"); params.append(person_id)
    return rows(db.execute(f"{INCOME_SELECT} WHERE {' AND '.join(where)} ORDER BY i.date DESC, i.id DESC", params))


@router.post("/income", status_code=201, tags=["income"])
def create_income(body: IncomeIn, db=DB):
    """Record one salary payment (or bonus) for one person. Call once per person per month."""
    pid = resolve_person(db, body.person_id, body.person, required=True)
    rate, chf = to_chf(db, body.amount, body.currency, body.date, body.fx_rate)
    cur = db.execute(
        "INSERT INTO income (person_id,date,amount,currency,fx_rate,amount_chf,gross_amount,kind,note) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (pid, body.date, body.amount, body.currency, rate, chf, body.gross_amount, body.kind, body.note))
    return one(db, f"{INCOME_SELECT} WHERE i.id=?", (cur.lastrowid,))


@router.patch("/income/{income_id}", tags=["income"])
def update_income(income_id: int, body: IncomePatch, db=DB):
    cur = one(db, "SELECT * FROM income WHERE id=?", (income_id,), "Income entry")
    f = body.model_dump(exclude_unset=True)
    if "person_id" in f:
        resolve_person(db, f["person_id"], None, required=True)
    if {"amount", "currency", "date", "fx_rate"} & f.keys():
        m = {**cur, **f}
        explicit = f.get("fx_rate") if "fx_rate" in f else (cur["fx_rate"] if not ({"currency", "date"} & f.keys()) else None)
        f["fx_rate"], f["amount_chf"] = to_chf(db, m["amount"], m["currency"], m["date"], explicit)
    update_row(db, "income", income_id, f)
    return one(db, f"{INCOME_SELECT} WHERE i.id=?", (income_id,))


@router.delete("/income/{income_id}", tags=["income"])
def delete_income(income_id: int, db=DB):
    return delete_row(db, "income", income_id, "Income entry")


@router.get("/income/summary", tags=["income"])
def income_summary(year: Optional[int] = None, db=DB):
    """Per-person income for a year: 12 monthly totals, total by kind, yearly total (CHF)."""
    year = year or _date.today().year
    people = rows(db.execute("SELECT id, name FROM people ORDER BY id"))
    data = rows(db.execute(
        "SELECT person_id, substr(date,6,2) AS m, kind, SUM(amount_chf) AS total, SUM(gross_amount) AS gross "
        "FROM income WHERE substr(date,1,4)=? GROUP BY person_id, m, kind", (str(year),)))
    out = []
    for p in people:
        monthly, by_kind, gross = [0.0] * 12, {}, 0.0
        for d in data:
            if d["person_id"] == p["id"]:
                monthly[int(d["m"]) - 1] += d["total"]
                by_kind[d["kind"]] = r2(by_kind.get(d["kind"], 0) + d["total"])
                gross += d["gross"] or 0
        out.append({"person_id": p["id"], "person": p["name"], "monthly_chf": [r2(x) for x in monthly],
                    "by_kind_chf": by_kind, "total_chf": r2(sum(monthly)), "gross_total": r2(gross)})
    years = [int(r["y"]) for r in db.execute("SELECT DISTINCT substr(date,1,4) AS y FROM income ORDER BY y")]
    return {"year": year, "people": out, "total_chf": r2(sum(p["total_chf"] for p in out)),
            "monthly_total_chf": [r2(sum(p["monthly_chf"][i] for p in out)) for i in range(12)],
            "years_with_data": years}


@router.get("/income/yearly", tags=["income"])
def income_yearly(db=DB):
    """Total income per person for every year on record (CHF)."""
    data = rows(db.execute(
        "SELECT substr(i.date,1,4) AS year, p.id AS person_id, p.name AS person, SUM(i.amount_chf) AS total_chf "
        "FROM income i JOIN people p ON p.id=i.person_id GROUP BY year, p.id ORDER BY year, p.id"))
    years: dict = {}
    for d in data:
        y = years.setdefault(d["year"], {"year": int(d["year"]), "people": [], "total_chf": 0})
        y["people"].append({"person_id": d["person_id"], "person": d["person"], "total_chf": r2(d["total_chf"])})
        y["total_chf"] = r2(y["total_chf"] + d["total_chf"])
    return list(years.values())


# ---------------------------------------------------------------- categories & budgets

class CategoryIn(BaseModel):
    name: str = Field(min_length=1)


class CategoryPatch(BaseModel):
    name: Optional[str] = Field(None, min_length=1)
    archived: Optional[bool] = None


class BudgetIn(BaseModel):
    category_id: Optional[int] = None
    category: Optional[str] = Field(None, description="Category name, alternative to category_id")
    amount: float = Field(ge=0, description="Monthly budget in CHF. 0 removes the budget from valid_from on.")
    valid_from: Optional[str] = Field(None, pattern=MONTH, description="YYYY-MM, defaults to the current month")


@router.get("/categories", tags=["categories"])
def list_categories(db=DB):
    """Expense categories. Use these names (or ids) when creating transactions."""
    return rows(db.execute("SELECT * FROM categories ORDER BY archived, name"))


@router.post("/categories", status_code=201, tags=["categories"])
def create_category(body: CategoryIn, db=DB):
    try:
        cur = db.execute("INSERT INTO categories (name) VALUES (?)", (body.name.strip(),))
    except sqlite3.IntegrityError:
        raise HTTPException(409, "A category with that name already exists")
    return one(db, "SELECT * FROM categories WHERE id=?", (cur.lastrowid,))


@router.patch("/categories/{category_id}", tags=["categories"])
def update_category(category_id: int, body: CategoryPatch, db=DB):
    one(db, "SELECT id FROM categories WHERE id=?", (category_id,), "Category")
    try:
        update_row(db, "categories", category_id, body.model_dump(exclude_unset=True))
    except sqlite3.IntegrityError:
        raise HTTPException(409, "A category with that name already exists")
    return one(db, "SELECT * FROM categories WHERE id=?", (category_id,))


@router.delete("/categories/{category_id}", tags=["categories"])
def delete_category(category_id: int, db=DB):
    """Delete a category. Its transactions are kept but become uncategorised; prefer archiving."""
    return delete_row(db, "categories", category_id, "Category")


def budget_status(db, month: str) -> list[dict]:
    cats = rows(db.execute("SELECT id, name, archived FROM categories ORDER BY name"))
    spent = {r["category_id"]: r for r in db.execute(
        "SELECT category_id, SUM(amount_chf) AS spent, COUNT(*) AS n FROM transactions "
        "WHERE substr(date,1,7)=? AND rollup_id IS NULL GROUP BY category_id", (month,))}
    out = []
    for c in cats:
        b = db.execute("SELECT amount FROM budgets WHERE category_id=? AND valid_from<=? "
                       "ORDER BY valid_from DESC LIMIT 1", (c["id"], month)).fetchone()
        budget = b["amount"] if b and b["amount"] > 0 else None
        s = spent.get(c["id"])
        sp = r2(s["spent"]) if s else 0.0
        if c["archived"] and not s:
            continue
        out.append({"category_id": c["id"], "category": c["name"], "budget_chf": budget, "spent_chf": sp,
                    "transactions": s["n"] if s else 0,
                    "remaining_chf": r2(budget - sp) if budget is not None else None,
                    "used_pct": round(sp / budget * 100, 1) if budget else None,
                    "over_budget": bool(budget is not None and sp > budget)})
    if None in spent:
        s = spent[None]
        out.append({"category_id": None, "category": "Uncategorised", "budget_chf": None, "spent_chf": r2(s["spent"]),
                    "transactions": s["n"], "remaining_chf": None, "used_pct": None, "over_budget": False})
    return out


@router.get("/budgets", tags=["budgets"])
def list_budgets(db=DB):
    """Every budget row ever set (the history). A row applies from valid_from until a newer row replaces it."""
    return rows(db.execute("SELECT b.*, c.name AS category FROM budgets b JOIN categories c ON c.id=b.category_id "
                           "ORDER BY c.name, b.valid_from"))


@router.put("/budgets", tags=["budgets"])
def set_budget(body: BudgetIn, db=DB):
    """Set the monthly CHF budget of a category from `valid_from` onwards (upsert)."""
    cid = resolve_category(db, body.category_id, body.category)
    if cid is None:
        raise HTTPException(422, "category_id or category (name) is required")
    month = body.valid_from or this_month()
    db.execute("INSERT INTO budgets (category_id, amount, valid_from) VALUES (?,?,?) "
               "ON CONFLICT(category_id, valid_from) DO UPDATE SET amount=excluded.amount", (cid, body.amount, month))
    return one(db, "SELECT b.*, c.name AS category FROM budgets b JOIN categories c ON c.id=b.category_id "
                   "WHERE b.category_id=? AND b.valid_from=?", (cid, month))


@router.delete("/budgets/{budget_id}", tags=["budgets"])
def delete_budget(budget_id: int, db=DB):
    return delete_row(db, "budgets", budget_id, "Budget")


@router.get("/budgets/status", tags=["budgets"])
def get_budget_status(month: Optional[str] = Query(None, pattern=MONTH), db=DB):
    """Budget vs actual spending per category for a month (default: current month)."""
    month = month or this_month()
    items = budget_status(db, month)
    budgeted = [i for i in items if i["budget_chf"] is not None]
    return {"month": month, "items": items,
            "total_budget_chf": r2(sum(i["budget_chf"] for i in budgeted)),
            "total_spent_in_budgeted_chf": r2(sum(i["spent_chf"] for i in budgeted)),
            "over_budget": [i["category"] for i in budgeted if i["over_budget"]]}


@router.get("/budgets/history", tags=["budgets"])
def budget_history(months: int = Query(12, ge=1, le=120), end: Optional[str] = Query(None, pattern=MONTH), db=DB):
    """Budget vs spent per category for each of the last `months` months."""
    return [{"month": m, "items": budget_status(db, m)} for m in last_months(end or this_month(), months)]


# ---------------------------------------------------------------- statements (bank screenshots)

@router.get("/statements", tags=["statements"])
def list_statements(month: Optional[str] = Query(None, pattern=MONTH), db=DB):
    """Uploaded bank screenshots / statements, with how many transactions reference each."""
    sql = ("SELECT s.*, (SELECT COUNT(*) FROM transactions t WHERE t.statement_id=s.id) AS transactions "
           "FROM statements s")
    if month:
        return rows(db.execute(sql + " WHERE s.month=? ORDER BY s.id DESC", (month,)))
    return rows(db.execute(sql + " ORDER BY s.id DESC"))


@router.post("/statements", status_code=201, tags=["statements"])
def upload_statement(file: UploadFile = File(...), month: str = Form(..., pattern=MONTH),
                           note: Optional[str] = Form(None), db=DB):
    """Upload a bank screenshot or PDF (multipart/form-data) so the source of a month's transactions is archived.

    The app does not read the image; extract the transactions yourself and post them to
    /api/transactions/bulk with the returned statement id.
    """
    ctype = file.content_type or ""
    if not (ctype.startswith("image/") or ctype == "application/pdf"):
        raise HTTPException(415, "Only images and PDFs are accepted")
    data = file.file.read()
    if len(data) > 25 * 1024 * 1024:
        raise HTTPException(413, "File larger than 25 MB")
    ext = Path(file.filename or "").suffix.lower()
    if ext not in (".png", ".jpg", ".jpeg", ".webp", ".gif", ".pdf", ".heic"):
        ext = ".pdf" if ctype == "application/pdf" else ".png"
    stored = f"{uuid.uuid4().hex}{ext}"
    try:
        dbm.save_file(stored, data)
    except Exception as e:
        raise HTTPException(503, f"Could not store the file: {e}")
    cur = db.execute("INSERT INTO statements (month, filename, stored_name, content_type, note) VALUES (?,?,?,?,?)",
                     (month, file.filename or stored, stored, ctype, note))
    return one(db, "SELECT * FROM statements WHERE id=?", (cur.lastrowid,))


@router.get("/statements/{statement_id}/file", tags=["statements"])
def get_statement_file(statement_id: int, db=DB):
    """Download the original uploaded file."""
    s = one(db, "SELECT * FROM statements WHERE id=?", (statement_id,), "Statement")
    data = dbm.read_file(s["stored_name"])
    if data is None:
        raise HTTPException(404, "The stored file is missing")
    return Response(data, media_type=s["content_type"] or "application/octet-stream",
                    headers={"Content-Disposition": f"inline; filename*=UTF-8''{quote(s['filename'])}"})


@router.delete("/statements/{statement_id}", tags=["statements"])
def delete_statement(statement_id: int, db=DB):
    """Delete the stored file. Transactions that referenced it are kept."""
    s = one(db, "SELECT * FROM statements WHERE id=?", (statement_id,), "Statement")
    dbm.delete_file(s["stored_name"])
    return delete_row(db, "statements", statement_id, "Statement")


# ---------------------------------------------------------------- transactions (expenses)

class TransactionIn(BaseModel):
    date: str = Field(pattern=DATE, description="Booking date YYYY-MM-DD")
    description: str = Field(min_length=1)
    amount: float = Field(description="Positive = money spent, negative = refund. In `currency`.")
    currency: str = Field("CHF", pattern=CUR, description="CHF or GBP (any ISO code works)")
    fx_rate: Optional[float] = Field(None, description="CHF per 1 unit of currency; omit to use stored/auto rate")
    category_id: Optional[int] = None
    category: Optional[str] = Field(None, description="Category name, alternative to category_id")
    person_id: Optional[int] = None
    person: Optional[str] = None
    recurring_id: Optional[int] = Field(None, description="Set when this payment settles a recurring charge")
    statement_id: Optional[int] = None
    account: Optional[str] = Field(None, description="Free text, e.g. 'UBS joint' or 'Monzo'")
    note: Optional[str] = None


class TransactionPatch(BaseModel):
    date: Optional[str] = Field(None, pattern=DATE)
    description: Optional[str] = Field(None, min_length=1)
    amount: Optional[float] = None
    currency: Optional[str] = Field(None, pattern=CUR)
    fx_rate: Optional[float] = None
    category_id: Optional[int] = None
    category: Optional[str] = None
    person_id: Optional[int] = None
    recurring_id: Optional[int] = None
    statement_id: Optional[int] = None
    account: Optional[str] = None
    note: Optional[str] = None


class BulkIn(BaseModel):
    transactions: list[TransactionIn]
    statement_id: Optional[int] = Field(None, description="Applied to every transaction that has none")
    skip_duplicates: bool = Field(True, description="Skip rows already stored with the same date, amount, "
                                                    "currency and description")


TX_SELECT = ("SELECT t.*, c.name AS category, p.name AS person, r.name AS recurring, "
             "EXISTS(SELECT 1 FROM transactions x WHERE x.rollup_id=t.id) AS is_rollup FROM transactions t "
             "LEFT JOIN categories c ON c.id=t.category_id LEFT JOIN people p ON p.id=t.person_id "
             "LEFT JOIN recurring r ON r.id=t.recurring_id")


def insert_tx(db, t: TransactionIn, default_statement: Optional[int] = None) -> int:
    cid = resolve_category(db, t.category_id, t.category)
    pid = resolve_person(db, t.person_id, t.person)
    if t.recurring_id is not None:
        rec = one(db, "SELECT * FROM recurring WHERE id=?", (t.recurring_id,), "Recurring charge")
        cid = cid if cid is not None else rec["category_id"]
    sid = t.statement_id if t.statement_id is not None else default_statement
    if sid is not None:
        one(db, "SELECT id FROM statements WHERE id=?", (sid,), "Statement")
    rate, chf = to_chf(db, t.amount, t.currency, t.date, t.fx_rate)
    cur = db.execute(
        "INSERT INTO transactions (date,description,amount,currency,fx_rate,amount_chf,category_id,person_id,"
        "recurring_id,statement_id,account,note) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (t.date, t.description.strip(), t.amount, t.currency, rate, chf, cid, pid, t.recurring_id, sid,
         t.account, t.note))
    return cur.lastrowid


@router.get("/transactions", tags=["transactions"])
def list_transactions(month: Optional[str] = Query(None, pattern=MONTH),
                      date_from: Optional[str] = Query(None, pattern=DATE),
                      date_to: Optional[str] = Query(None, pattern=DATE),
                      category_id: Optional[int] = None, uncategorised: bool = False,
                      currency: Optional[str] = Query(None, pattern=CUR),
                      recurring_id: Optional[int] = None, statement_id: Optional[int] = None,
                      q: Optional[str] = Query(None, description="Text search in description/note"),
                      limit: int = Query(1000, ge=1, le=10000), db=DB):
    """Expense transactions, newest first. All filters are optional and combine with AND."""
    where, params = ["1=1"], []
    for cond, val in [("substr(t.date,1,7)=?", month), ("t.date>=?", date_from), ("t.date<=?", date_to),
                      ("t.category_id=?", category_id), ("t.currency=?", currency),
                      ("t.recurring_id=?", recurring_id), ("t.statement_id=?", statement_id)]:
        if val is not None:
            where.append(cond); params.append(val)
    if uncategorised:
        where.append("t.category_id IS NULL")
    if q:
        where.append("(t.description LIKE ? OR t.note LIKE ?)"); params += [f"%{q}%", f"%{q}%"]
    return rows(db.execute(f"{TX_SELECT} WHERE {' AND '.join(where)} ORDER BY t.date DESC, t.id DESC LIMIT ?",
                           (*params, limit)))


@router.post("/transactions", status_code=201, tags=["transactions"])
def create_transaction(body: TransactionIn, db=DB):
    """Record a single expense."""
    return one(db, f"{TX_SELECT} WHERE t.id=?", (insert_tx(db, body),))


@router.post("/transactions/bulk", status_code=201, tags=["transactions"])
def create_transactions_bulk(body: BulkIn, db=DB):
    """Record many expenses at once (the normal way to load a month from a bank screenshot).

    All-or-nothing: if any row is invalid nothing is stored and the error names the row index.
    Safe to retry: with skip_duplicates (default) rows that already existed before this call are skipped.
    """
    if body.statement_id is not None:
        one(db, "SELECT id FROM statements WHERE id=?", (body.statement_id,), "Statement")
    existing = set()
    if body.skip_duplicates and body.transactions:
        dates = [t.date for t in body.transactions]
        for r in db.execute("SELECT date, amount, currency, lower(description) AS d FROM transactions "
                            "WHERE date BETWEEN ? AND ?", (min(dates), max(dates))):
            existing.add((r["date"], round(r["amount"], 2), r["currency"], r["d"]))
    created, skipped = [], []
    for i, t in enumerate(body.transactions):
        if (t.date, round(t.amount, 2), t.currency, t.description.strip().lower()) in existing:
            skipped.append({"index": i, "date": t.date, "description": t.description, "amount": t.amount})
            continue
        try:
            created.append(insert_tx(db, t, body.statement_id))
        except HTTPException as e:
            raise HTTPException(e.status_code, f"transactions[{i}]: {e.detail}")
    out = []
    if created:
        marks = ",".join("?" * len(created))
        out = rows(db.execute(f"{TX_SELECT} WHERE t.id IN ({marks}) ORDER BY t.date, t.id", created))
    return {"created_count": len(out), "skipped_duplicates_count": len(skipped),
            "total_created_chf": r2(sum(t["amount_chf"] for t in out)), "created": out, "skipped_duplicates": skipped}


class RollupIn(BaseModel):
    month: Optional[str] = Field(None, pattern=MONTH, description="Month to pull; default current month")
    currency: str = Field("GBP", pattern=CUR)
    fx_rate: Optional[float] = Field(None, gt=0, description="CHF per 1 unit; default is today's rate")
    fee_pct: float = Field(DEFAULT_TRANSFER_FEE_PCT, ge=0, le=100,
                           description="Added on top for transfer fees (Wise), in percent")
    date: Optional[str] = Field(None, pattern=DATE, description="Date of the summary line; default today, "
                                                                "or the last day of the month for a past month")


def rollup_name(currency: str) -> str:
    return "UK expenses" if currency == "GBP" else f"{currency} expenses"


def rollup_state(db, month: str, currency: str) -> dict:
    r = db.execute("SELECT COALESCE(SUM(amount),0) AS total, COUNT(*) AS n, "
                   "COALESCE(SUM(rollup_id IS NULL),0) AS not_pulled FROM transactions "
                   "WHERE currency=? AND substr(date,1,7)=?", (currency, month)).fetchone()
    ids = [x[0] for x in db.execute("SELECT DISTINCT rollup_id FROM transactions WHERE currency=? "
                                    "AND substr(date,1,7)=? AND rollup_id IS NOT NULL", (currency, month))]
    return {"total": r2(r["total"]), "transactions": r["n"], "not_yet_pulled": r["not_pulled"], "line_ids": ids}


@router.get("/transactions/rollup", tags=["transactions"])
def preview_rollup(month: Optional[str] = Query(None, pattern=MONTH), currency: str = Query("GBP", pattern=CUR),
                   db=DB):
    """What POST /transactions/rollup would do for a month: the foreign total, today's rate, the default
    fee and the resulting CHF amount, plus the summary line that already exists (if any)."""
    month = month or this_month()
    st = rollup_state(db, month, currency)
    try:
        rate = resolve_rate(db, currency, _date.today().isoformat())
    except HTTPException:
        rate = None
    existing = one(db, f"{TX_SELECT} WHERE t.id=?", (st["line_ids"][0],)) if st["line_ids"] else None
    return {"month": month, "currency": currency, "total": st["total"], "transactions": st["transactions"],
            "not_yet_pulled": st["not_yet_pulled"], "rate": rate, "fee_pct": DEFAULT_TRANSFER_FEE_PCT,
            "amount_chf": r2(st["total"] * rate * (1 + DEFAULT_TRANSFER_FEE_PCT / 100)) if rate else None,
            "existing": existing}


@router.post("/transactions/rollup", status_code=201, tags=["transactions"])
def create_rollup(body: RollupIn = RollupIn(), db=DB):
    """Pull a month's foreign-currency expenses (default GBP) into ONE CHF line called "UK expenses".

    amount = total in the foreign currency x today's exchange rate x (1 + fee_pct/100). The fee
    (default 10 %) covers what it costs to send the money abroad. The individual foreign expenses
    stay on record (GBP screen, foreign reports) but no longer count towards CHF totals, so nothing
    is counted twice. Calling it again for the same month replaces the line with a fresh one;
    deleting the line puts the individual expenses back into the CHF totals.
    """
    if body.currency == BASE:
        raise HTTPException(422, "Choose a foreign currency; CHF expenses need no conversion")
    month = body.month or this_month()
    st = rollup_state(db, month, body.currency)
    if not st["transactions"]:
        raise HTTPException(422, f"There are no {body.currency} expenses in {month}")
    today = _date.today().isoformat()
    rate = resolve_rate(db, body.currency, today, body.fx_rate)
    if body.date:
        if body.date[:7] != month:
            raise HTTPException(422, f"date must be in {month}")
        line_date = body.date
    else:
        y, m = int(month[:4]), int(month[5:7])
        line_date = today if today[:7] == month else f"{month}-{calendar.monthrange(y, m)[1]:02d}"
    name = rollup_name(body.currency)
    cat = db.execute("SELECT id FROM categories WHERE name=? COLLATE NOCASE", (name,)).fetchone()
    cid = cat["id"] if cat else db.execute("INSERT INTO categories (name) VALUES (?)", (name,)).lastrowid
    if st["line_ids"]:
        marks = ",".join("?" * len(st["line_ids"]))
        db.execute(f"DELETE FROM transactions WHERE id IN ({marks})", st["line_ids"])
    chf = round(st["total"] * rate * (1 + body.fee_pct / 100), 2)
    note = (f"{body.currency} {st['total']:.2f} x {rate:.4f} + {body.fee_pct:g}% transfer fees "
            f"({st['transactions']} expenses, rate of {today})")
    cur = db.execute("INSERT INTO transactions (date,description,amount,currency,fx_rate,amount_chf,category_id,note) "
                     "VALUES (?,?,?,'CHF',1,?,?,?)", (line_date, name, chf, chf, cid, note))
    db.execute("UPDATE transactions SET rollup_id=? WHERE currency=? AND substr(date,1,7)=?",
               (cur.lastrowid, body.currency, month))
    return {**one(db, f"{TX_SELECT} WHERE t.id=?", (cur.lastrowid,)),
            "pulled": {"currency": body.currency, "total": st["total"], "transactions": st["transactions"],
                       "rate": rate, "fee_pct": body.fee_pct, "replaced_existing_line": bool(st["line_ids"])}}


@router.patch("/transactions/{tx_id}", tags=["transactions"])
def update_transaction(tx_id: int, body: TransactionPatch, db=DB):
    """Change any field, e.g. re-categorise. Send category_id: null to clear the category."""
    cur = one(db, "SELECT * FROM transactions WHERE id=?", (tx_id,), "Transaction")
    f = body.model_dump(exclude_unset=True)
    if "category" in f:
        name = f.pop("category")
        if name:
            f["category_id"] = resolve_category(db, None, name)
    elif f.get("category_id") is not None:
        resolve_category(db, f["category_id"], None)
    if {"amount", "currency", "date", "fx_rate"} & f.keys():
        m = {**cur, **f}
        explicit = f.get("fx_rate") if "fx_rate" in f else (cur["fx_rate"] if not ({"currency", "date"} & f.keys()) else None)
        f["fx_rate"], f["amount_chf"] = to_chf(db, m["amount"], m["currency"], m["date"], explicit)
    try:
        update_row(db, "transactions", tx_id, f)
    except sqlite3.IntegrityError:
        raise HTTPException(422, "person_id, recurring_id or statement_id does not exist")
    return one(db, f"{TX_SELECT} WHERE t.id=?", (tx_id,))


@router.delete("/transactions/{tx_id}", tags=["transactions"])
def delete_transaction(tx_id: int, db=DB):
    return delete_row(db, "transactions", tx_id, "Transaction")


# ---------------------------------------------------------------- recurring charges

class RecurringIn(BaseModel):
    name: str = Field(min_length=1, description="e.g. Rent, Car lease")
    amount: float = Field(gt=0, description="Expected amount per month, in `currency`")
    currency: str = Field("CHF", pattern=CUR)
    category_id: Optional[int] = None
    category: Optional[str] = None
    day_of_month: Optional[int] = Field(None, ge=1, le=31)
    start_month: Optional[str] = Field(None, pattern=MONTH)
    end_month: Optional[str] = Field(None, pattern=MONTH)
    note: Optional[str] = None


class RecurringPatch(BaseModel):
    name: Optional[str] = Field(None, min_length=1)
    amount: Optional[float] = Field(None, gt=0)
    currency: Optional[str] = Field(None, pattern=CUR)
    category_id: Optional[int] = None
    day_of_month: Optional[int] = Field(None, ge=1, le=31)
    start_month: Optional[str] = Field(None, pattern=MONTH)
    end_month: Optional[str] = Field(None, pattern=MONTH)
    active: Optional[bool] = None
    note: Optional[str] = None


class PayIn(BaseModel):
    month: Optional[str] = Field(None, pattern=MONTH, description="Month being settled; default current month")
    date: Optional[str] = Field(None, pattern=DATE, description="Actual payment date; default day_of_month in month")
    amount: Optional[float] = Field(None, description="Actual amount if it differs from the expected amount")
    fx_rate: Optional[float] = None
    statement_id: Optional[int] = None
    note: Optional[str] = None


REC_SELECT = "SELECT r.*, c.name AS category FROM recurring r LEFT JOIN categories c ON c.id=r.category_id"


def recurring_status(db, month: str) -> dict:
    recs = rows(db.execute(
        f"{REC_SELECT} WHERE r.active=1 AND (r.start_month IS NULL OR r.start_month<=?) "
        "AND (r.end_month IS NULL OR r.end_month>=?) ORDER BY r.day_of_month IS NULL, r.day_of_month, r.name",
        (month, month)))
    paid = {r["recurring_id"]: r for r in db.execute(
        "SELECT recurring_id, SUM(amount) AS amount, SUM(amount_chf) AS chf, MAX(date) AS date, COUNT(*) AS n "
        "FROM transactions WHERE recurring_id IS NOT NULL AND substr(date,1,7)=? GROUP BY recurring_id", (month,))}
    items = []
    for r in recs:
        try:
            expected_chf = r2(r["amount"] * resolve_rate(db, r["currency"], f"{month}-15"))
        except HTTPException:
            expected_chf = None
        p = paid.get(r["id"])
        items.append({"recurring_id": r["id"], "name": r["name"], "category": r["category"],
                      "currency": r["currency"], "day_of_month": r["day_of_month"],
                      "expected_amount": r["amount"], "expected_chf": expected_chf,
                      "paid": bool(p), "paid_amount": r2(p["amount"]) if p else 0.0,
                      "paid_chf": r2(p["chf"]) if p else 0.0, "paid_date": p["date"] if p else None,
                      "difference": r2(p["amount"] - r["amount"]) if p else None})
    return {"month": month, "items": items,
            "expected_chf": r2(sum(i["expected_chf"] or 0 for i in items)),
            "paid_chf": r2(sum(i["paid_chf"] for i in items)),
            "unpaid_chf": r2(sum(i["expected_chf"] or 0 for i in items if not i["paid"])),
            "unpaid": [i["name"] for i in items if not i["paid"]]}


@router.get("/recurring", tags=["recurring"])
def list_recurring(db=DB):
    """All standing monthly charges (rent, car payment, ...), including inactive ones."""
    return rows(db.execute(f"{REC_SELECT} ORDER BY r.active DESC, r.name"))


@router.post("/recurring", status_code=201, tags=["recurring"])
def create_recurring(body: RecurringIn, db=DB):
    cid = resolve_category(db, body.category_id, body.category)
    cur = db.execute(
        "INSERT INTO recurring (name,amount,currency,category_id,day_of_month,start_month,end_month,note) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (body.name.strip(), body.amount, body.currency, cid, body.day_of_month, body.start_month,
         body.end_month, body.note))
    return one(db, f"{REC_SELECT} WHERE r.id=?", (cur.lastrowid,))


@router.get("/recurring/status", tags=["recurring"])
def get_recurring_status(month: Optional[str] = Query(None, pattern=MONTH), db=DB):
    """For a month: each recurring charge, whether it was paid, how much, and what is still outstanding."""
    return recurring_status(db, month or this_month())


@router.patch("/recurring/{recurring_id}", tags=["recurring"])
def update_recurring(recurring_id: int, body: RecurringPatch, db=DB):
    """Edit a charge. To stop one, set end_month (keeps history) or active=false."""
    one(db, "SELECT id FROM recurring WHERE id=?", (recurring_id,), "Recurring charge")
    f = body.model_dump(exclude_unset=True)
    if f.get("category_id") is not None:
        resolve_category(db, f["category_id"], None)
    update_row(db, "recurring", recurring_id, f)
    return one(db, f"{REC_SELECT} WHERE r.id=?", (recurring_id,))


@router.delete("/recurring/{recurring_id}", tags=["recurring"])
def delete_recurring(recurring_id: int, db=DB):
    """Delete the definition. Past payments stay as ordinary transactions."""
    return delete_row(db, "recurring", recurring_id, "Recurring charge")


@router.post("/recurring/{recurring_id}/pay", status_code=201, tags=["recurring"])
def pay_recurring(recurring_id: int, body: PayIn = PayIn(), db=DB):
    """Mark a recurring charge as paid for a month by creating its transaction.

    Returns 409 if it is already paid for that month. Alternatively, post a normal
    transaction with recurring_id set.
    """
    rec = one(db, "SELECT * FROM recurring WHERE id=?", (recurring_id,), "Recurring charge")
    month = body.month or (body.date[:7] if body.date else this_month())
    if db.execute("SELECT 1 FROM transactions WHERE recurring_id=? AND substr(date,1,7)=?",
                  (recurring_id, month)).fetchone():
        raise HTTPException(409, f"'{rec['name']}' is already marked paid for {month}")
    day = min(rec["day_of_month"] or 1, 28)
    t = TransactionIn(date=body.date or f"{month}-{day:02d}", description=rec["name"],
                      amount=body.amount if body.amount is not None else rec["amount"],
                      currency=rec["currency"], fx_rate=body.fx_rate, recurring_id=recurring_id,
                      statement_id=body.statement_id, note=body.note)
    return one(db, f"{TX_SELECT} WHERE t.id=?", (insert_tx(db, t),))


# ---------------------------------------------------------------- FX rates

class FxIn(BaseModel):
    currency: str = Field("GBP", pattern=CUR)
    date: str = Field(pattern=DATE)
    rate: float = Field(gt=0, description="CHF per 1 unit of currency, e.g. 1.12 for GBP")


@router.get("/fx", tags=["fx"])
def list_fx(currency: str = Query("GBP", pattern=CUR), db=DB):
    """Stored exchange rates to CHF for a currency, oldest first."""
    return rows(db.execute("SELECT * FROM fx_rates WHERE currency=? ORDER BY date", (currency,)))


@router.put("/fx", tags=["fx"])
def set_fx(body: FxIn, db=DB):
    """Store (or overwrite) the rate for a date. Existing transactions are not re-converted."""
    if body.currency == BASE:
        raise HTTPException(422, "CHF is the base currency")
    db.execute("INSERT OR REPLACE INTO fx_rates (currency, date, rate, source) VALUES (?,?,?,'manual')",
               (body.currency, body.date, body.rate))
    return body.model_dump()


@router.get("/fx/rate", tags=["fx"])
def get_fx_rate(currency: str = Query("GBP", pattern=CUR), date: Optional[str] = Query(None, pattern=DATE), db=DB):
    """The rate the app would use to convert `currency` to CHF on `date` (default today)."""
    d = date or _date.today().isoformat()
    return {"currency": currency, "date": d, "rate": resolve_rate(db, currency, d)}


# ---------------------------------------------------------------- investments

ACCOUNT_TYPES = Literal["cash", "savings", "stocks", "pension", "crypto", "property", "other"]


class AccountIn(BaseModel):
    name: str = Field(min_length=1, description="e.g. Emergency cash, IBKR stocks")
    type: ACCOUNT_TYPES = "cash"
    currency: str = Field("CHF", pattern=CUR)
    institution: Optional[str] = None
    person_id: Optional[int] = Field(None, description="Owner; omit for joint accounts")
    note: Optional[str] = None


class AccountPatch(BaseModel):
    name: Optional[str] = Field(None, min_length=1)
    type: Optional[ACCOUNT_TYPES] = None
    institution: Optional[str] = None
    person_id: Optional[int] = None
    active: Optional[bool] = None
    note: Optional[str] = None


class ValuationIn(BaseModel):
    date: str = Field(pattern=DATE)
    value: float = Field(description="Total account value on that date, in the account's currency")
    fx_rate: Optional[float] = None


class FlowIn(BaseModel):
    date: str = Field(pattern=DATE)
    amount: float = Field(description="Positive = money paid in, negative = money taken out (account currency)")
    fx_rate: Optional[float] = None
    note: Optional[str] = None


def account_summary(db, a: dict) -> dict:
    vals = rows(db.execute("SELECT * FROM valuations WHERE account_id=? ORDER BY date DESC LIMIT 2", (a["id"],)))
    fl = db.execute("SELECT COALESCE(SUM(amount),0) AS amt, COALESCE(SUM(amount_chf),0) AS chf, COUNT(*) AS n "
                    "FROM flows WHERE account_id=?", (a["id"],)).fetchone()
    latest = vals[0] if vals else None
    out = {**a, "latest_date": latest["date"] if latest else None,
           "value": latest["value"] if latest else None,
           "value_chf": latest["value_chf"] if latest else None,
           "previous_value": vals[1]["value"] if len(vals) > 1 else None,
           "previous_date": vals[1]["date"] if len(vals) > 1 else None,
           "net_invested": r2(fl["amt"]), "net_invested_chf": r2(fl["chf"]),
           "gain": None, "gain_chf": None, "return_pct": None}
    if latest and fl["n"]:
        out["gain"] = r2(latest["value"] - fl["amt"])
        out["gain_chf"] = r2(latest["value_chf"] - fl["chf"])
        if fl["amt"] > 0:
            out["return_pct"] = round((latest["value"] - fl["amt"]) / fl["amt"] * 100, 2)
    return out


def get_account(db, account_id: int) -> dict:
    return one(db, "SELECT a.*, p.name AS person FROM accounts a LEFT JOIN people p ON p.id=a.person_id "
                   "WHERE a.id=?", (account_id,), "Account")


@router.get("/accounts", tags=["investments"])
def list_accounts(db=DB):
    """Savings / investment accounts with latest value, net money paid in, gain and simple return."""
    accs = rows(db.execute("SELECT a.*, p.name AS person FROM accounts a LEFT JOIN people p ON p.id=a.person_id "
                           "ORDER BY a.active DESC, a.name"))
    return [account_summary(db, a) for a in accs]


@router.post("/accounts", status_code=201, tags=["investments"])
def create_account(body: AccountIn, db=DB):
    resolve_person(db, body.person_id, None)
    try:
        cur = db.execute("INSERT INTO accounts (name,type,currency,institution,person_id,note) VALUES (?,?,?,?,?,?)",
                         (body.name.strip(), body.type, body.currency, body.institution, body.person_id, body.note))
    except sqlite3.IntegrityError:
        raise HTTPException(409, "An account with that name already exists")
    return account_summary(db, get_account(db, cur.lastrowid))


@router.patch("/accounts/{account_id}", tags=["investments"])
def update_account(account_id: int, body: AccountPatch, db=DB):
    """Edit an account. Set active=false to close it (history is kept, it leaves the current net worth)."""
    get_account(db, account_id)
    try:
        update_row(db, "accounts", account_id, body.model_dump(exclude_unset=True))
    except sqlite3.IntegrityError:
        raise HTTPException(409, "Name already used or person does not exist")
    return account_summary(db, get_account(db, account_id))


@router.delete("/accounts/{account_id}", tags=["investments"])
def delete_account(account_id: int, db=DB):
    """Permanently delete an account with all its valuations and flows. Prefer active=false."""
    return delete_row(db, "accounts", account_id, "Account")


@router.get("/accounts/{account_id}/valuations", tags=["investments"])
def list_valuations(account_id: int, db=DB):
    get_account(db, account_id)
    return rows(db.execute("SELECT * FROM valuations WHERE account_id=? ORDER BY date DESC", (account_id,)))


@router.post("/accounts/{account_id}/valuations", status_code=201, tags=["investments"])
def add_valuation(account_id: int, body: ValuationIn, db=DB):
    """Record what the account was worth on a date (one per date; re-posting a date overwrites it)."""
    a = get_account(db, account_id)
    rate, chf = to_chf(db, body.value, a["currency"], body.date, body.fx_rate)
    db.execute("INSERT INTO valuations (account_id,date,value,fx_rate,value_chf) VALUES (?,?,?,?,?) "
               "ON CONFLICT(account_id,date) DO UPDATE SET value=excluded.value, fx_rate=excluded.fx_rate, "
               "value_chf=excluded.value_chf", (account_id, body.date, body.value, rate, chf))
    return account_summary(db, a)


@router.delete("/valuations/{valuation_id}", tags=["investments"])
def delete_valuation(valuation_id: int, db=DB):
    return delete_row(db, "valuations", valuation_id, "Valuation")


@router.get("/accounts/{account_id}/flows", tags=["investments"])
def list_flows(account_id: int, db=DB):
    get_account(db, account_id)
    return rows(db.execute("SELECT * FROM flows WHERE account_id=? ORDER BY date DESC, id DESC", (account_id,)))


@router.post("/accounts/{account_id}/flows", status_code=201, tags=["investments"])
def add_flow(account_id: int, body: FlowIn, db=DB):
    """Record money moved into (+) or out of (-) an account, e.g. the monthly savings transfer.

    Flows are what separates "we paid more in" from "the investments grew": gain = value - net flows.
    """
    a = get_account(db, account_id)
    rate, chf = to_chf(db, body.amount, a["currency"], body.date, body.fx_rate)
    cur = db.execute("INSERT INTO flows (account_id,date,amount,fx_rate,amount_chf,note) VALUES (?,?,?,?,?,?)",
                     (account_id, body.date, body.amount, rate, chf, body.note))
    return one(db, "SELECT * FROM flows WHERE id=?", (cur.lastrowid,))


@router.delete("/flows/{flow_id}", tags=["investments"])
def delete_flow(flow_id: int, db=DB):
    return delete_row(db, "flows", flow_id, "Flow")


def investment_history(db, months: list[str]) -> dict:
    accs = rows(db.execute("SELECT id, name, type, active FROM accounts ORDER BY name"))
    series = []
    for a in accs:
        vals = rows(db.execute("SELECT date, value_chf FROM valuations WHERE account_id=? ORDER BY date", (a["id"],)))
        fls = rows(db.execute("SELECT date, amount_chf FROM flows WHERE account_id=? ORDER BY date", (a["id"],)))
        v_out, i_out = [], []
        for m in months:
            end = m + "-31"
            v = [x["value_chf"] for x in vals if x["date"] <= end]
            v_out.append(r2(v[-1]) if v else None)
            i_out.append(r2(sum(x["amount_chf"] for x in fls if x["date"] <= end)) if fls else None)
        if any(x is not None for x in v_out):
            series.append({"account_id": a["id"], "name": a["name"], "type": a["type"], "active": a["active"],
                           "value_chf": v_out, "net_invested_chf": i_out})
    total = [r2(sum(s["value_chf"][i] or 0 for s in series)) for i in range(len(months))]
    return {"months": months, "accounts": series, "total_value_chf": total}


@router.get("/investments/summary", tags=["investments"])
def investments_summary(db=DB):
    """Current net worth across active accounts, split by account type, with gains where flows are recorded."""
    accs = [a for a in list_accounts(db) if a["active"]]
    by_type: dict = {}
    for a in accs:
        by_type[a["type"]] = r2(by_type.get(a["type"], 0) + (a["value_chf"] or 0))
    tracked = [a for a in accs if a["gain_chf"] is not None]
    invested = sum(a["net_invested_chf"] for a in tracked)
    gain = sum(a["gain_chf"] for a in tracked)
    return {"total_value_chf": r2(sum(a["value_chf"] or 0 for a in accs)), "by_type_chf": by_type,
            "net_invested_chf": r2(invested), "gain_chf": r2(gain),
            "return_pct": round(gain / invested * 100, 2) if invested > 0 else None, "accounts": accs}


@router.get("/investments/history", tags=["investments"])
def get_investment_history(months: int = Query(24, ge=1, le=240), end: Optional[str] = Query(None, pattern=MONTH),
                           db=DB):
    """Month-end value per account (last known value carried forward) and cumulative net money paid in."""
    return investment_history(db, last_months(end or this_month(), months))


# ---------------------------------------------------------------- reports

def month_totals(db, month: str) -> dict:
    inc = db.execute("SELECT COALESCE(SUM(amount_chf),0) FROM income WHERE substr(date,1,7)=?", (month,)).fetchone()[0]
    exp = db.execute("SELECT COALESCE(SUM(amount_chf),0) FROM transactions WHERE substr(date,1,7)=? "
                     "AND rollup_id IS NULL", (month,)).fetchone()[0]
    return {"month": month, "income_chf": r2(inc), "expenses_chf": r2(exp), "net_chf": r2(inc - exp),
            "savings_rate_pct": round((inc - exp) / inc * 100, 1) if inc > 0 else None}


@router.get("/reports/monthly", tags=["reports"])
def monthly_report(month: Optional[str] = Query(None, pattern=MONTH), db=DB):
    """Everything needed to close a month in one call: income per person, spending per category vs budget,
    recurring charges paid/unpaid, foreign-currency spending, and how much is left to send to savings."""
    month = month or this_month()
    totals = month_totals(db, month)
    people = rows(db.execute(
        "SELECT p.id AS person_id, p.name AS person, COALESCE(SUM(i.amount_chf),0) AS total_chf, COUNT(i.id) AS entries "
        "FROM people p LEFT JOIN income i ON i.person_id=p.id AND substr(i.date,1,7)=? "
        "WHERE p.active=1 OR i.id IS NOT NULL GROUP BY p.id ORDER BY p.id", (month,)))
    for p in people:
        p["total_chf"] = r2(p["total_chf"])
    rec = recurring_status(db, month)
    budgets = budget_status(db, month)
    foreign = rows(db.execute(
        "SELECT currency, SUM(amount) AS total, SUM(amount_chf) AS total_chf, COUNT(*) AS transactions "
        "FROM transactions WHERE substr(date,1,7)=? AND currency<>'CHF' GROUP BY currency", (month,)))
    for f in foreign:
        f["total"], f["total_chf"] = r2(f["total"]), r2(f["total_chf"])
    n = db.execute("SELECT COUNT(*) FROM transactions WHERE substr(date,1,7)=? AND rollup_id IS NULL",
                   (month,)).fetchone()[0]
    rec_counted = db.execute("SELECT COALESCE(SUM(amount_chf),0) FROM transactions WHERE recurring_id IS NOT NULL "
                             "AND rollup_id IS NULL AND substr(date,1,7)=?", (month,)).fetchone()[0]
    transferred = db.execute("SELECT COALESCE(SUM(amount_chf),0) FROM flows WHERE substr(date,1,7)=?",
                             (month,)).fetchone()[0]
    after_unpaid = r2(totals["net_chf"] - rec["unpaid_chf"])
    return {
        "month": month,
        "income": {"total_chf": totals["income_chf"], "by_person": people,
                   "missing_for": [p["person"] for p in people if p["entries"] == 0]},
        "expenses": {"total_chf": totals["expenses_chf"], "transactions": n,
                     "recurring_chf": r2(rec_counted), "variable_chf": r2(totals["expenses_chf"] - rec_counted),
                     "by_category": budgets, "foreign_currency": foreign},
        "recurring": rec,
        "budgets": {"over_budget": [b["category"] for b in budgets if b["over_budget"]],
                    "total_budget_chf": r2(sum(b["budget_chf"] or 0 for b in budgets))},
        "savings": {
            "net_chf": totals["net_chf"],
            "savings_rate_pct": totals["savings_rate_pct"],
            "unpaid_recurring_chf": rec["unpaid_chf"],
            "available_to_save_chf": after_unpaid,
            "already_transferred_chf": r2(transferred),
            "remaining_to_transfer_chf": r2(after_unpaid - transferred),
            "explanation": "available_to_save = income - expenses recorded - recurring charges not yet paid. "
                           "already_transferred = investment account flows dated in this month.",
        },
    }


@router.get("/reports/overview", tags=["reports"])
def overview(months: int = Query(12, ge=1, le=120), end: Optional[str] = Query(None, pattern=MONTH), db=DB):
    """Time series for dashboards: income, expenses, net and savings rate per month, plus net worth history."""
    ms = last_months(end or this_month(), months)
    series = [month_totals(db, m) for m in ms]
    return {"months": ms, "series": series, "net_worth": investment_history(db, ms),
            "totals": {"income_chf": r2(sum(s["income_chf"] for s in series)),
                       "expenses_chf": r2(sum(s["expenses_chf"] for s in series)),
                       "net_chf": r2(sum(s["net_chf"] for s in series))}}


@router.get("/reports/categories", tags=["reports"])
def category_trend(months: int = Query(12, ge=1, le=120), end: Optional[str] = Query(None, pattern=MONTH), db=DB):
    """Spending per category per month (CHF) for trend charts."""
    ms = last_months(end or this_month(), months)
    data = rows(db.execute(
        "SELECT substr(t.date,1,7) AS m, COALESCE(c.name,'Uncategorised') AS category, SUM(t.amount_chf) AS total "
        "FROM transactions t LEFT JOIN categories c ON c.id=t.category_id "
        "WHERE substr(t.date,1,7) BETWEEN ? AND ? AND t.rollup_id IS NULL GROUP BY m, category", (ms[0], ms[-1])))
    cats: dict = {}
    for d in data:
        cats.setdefault(d["category"], [0.0] * len(ms))[ms.index(d["m"])] = r2(d["total"])
    ordered = sorted(cats.items(), key=lambda kv: -sum(kv[1]))
    return {"months": ms, "categories": [{"category": k, "spent_chf": v, "total_chf": r2(sum(v))} for k, v in ordered]}


@router.get("/reports/yearly", tags=["reports"])
def yearly_report(year: Optional[int] = None, db=DB):
    """A calendar year at a glance: income, expenses, net per month and in total, spending by category."""
    year = year or _date.today().year
    ms = [f"{year}-{m:02d}" for m in range(1, 13)]
    series = [month_totals(db, m) for m in ms]
    cats = rows(db.execute(
        "SELECT COALESCE(c.name,'Uncategorised') AS category, SUM(t.amount_chf) AS total_chf FROM transactions t "
        "LEFT JOIN categories c ON c.id=t.category_id WHERE substr(t.date,1,4)=? AND t.rollup_id IS NULL "
        "GROUP BY category "
        "ORDER BY total_chf DESC", (str(year),)))
    inc, exp = sum(s["income_chf"] for s in series), sum(s["expenses_chf"] for s in series)
    return {"year": year, "income_chf": r2(inc), "expenses_chf": r2(exp), "net_chf": r2(inc - exp),
            "savings_rate_pct": round((inc - exp) / inc * 100, 1) if inc > 0 else None,
            "months": series, "by_category": [{**c, "total_chf": r2(c["total_chf"])} for c in cats],
            "income": income_summary(year, db)}


@router.get("/reports/foreign", tags=["reports"])
def foreign_report(currency: str = Query("GBP", pattern=CUR), months: int = Query(12, ge=1, le=120),
                   end: Optional[str] = Query(None, pattern=MONTH), db=DB):
    """Spending in a foreign currency (default GBP) per month, in that currency and in CHF."""
    ms = last_months(end or this_month(), months)
    data = {r["m"]: r for r in db.execute(
        "SELECT substr(date,1,7) AS m, SUM(amount) AS total, SUM(amount_chf) AS total_chf, COUNT(*) AS n "
        "FROM transactions WHERE currency=? AND substr(date,1,7) BETWEEN ? AND ? GROUP BY m",
        (currency, ms[0], ms[-1]))}
    series = [{"month": m, "total": r2(data[m]["total"]) if m in data else 0.0,
               "total_chf": r2(data[m]["total_chf"]) if m in data else 0.0,
               "transactions": data[m]["n"] if m in data else 0,
               "avg_rate": round(data[m]["total_chf"] / data[m]["total"], 4) if m in data and data[m]["total"] else None}
              for m in ms]
    recurring = rows(db.execute(f"{REC_SELECT} WHERE r.currency=? AND r.active=1 ORDER BY r.name", (currency,)))
    return {"currency": currency, "months": ms, "series": series,
            "total": r2(sum(s["total"] for s in series)), "total_chf": r2(sum(s["total_chf"] for s in series)),
            "recurring": recurring}
