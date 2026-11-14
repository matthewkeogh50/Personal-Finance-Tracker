"""SQLite storage: schema, connection handling and first-run seed data."""
import os
import sqlite3
import threading
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path

DATA_DIR = Path(os.environ.get("DATA_DIR", "./data"))
DB_PATH = DATA_DIR / "finance.db"
UPLOAD_DIR = DATA_DIR / "uploads"

SCHEMA = """
CREATE TABLE IF NOT EXISTS people (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS categories (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE COLLATE NOCASE,
    archived INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS budgets (
    id INTEGER PRIMARY KEY,
    category_id INTEGER NOT NULL REFERENCES categories(id) ON DELETE CASCADE,
    amount REAL NOT NULL,
    valid_from TEXT NOT NULL,              -- YYYY-MM, applies from this month onwards
    UNIQUE (category_id, valid_from)
);
CREATE TABLE IF NOT EXISTS income (
    id INTEGER PRIMARY KEY,
    person_id INTEGER NOT NULL REFERENCES people(id),
    date TEXT NOT NULL,                    -- YYYY-MM-DD
    amount REAL NOT NULL,                  -- net amount received, in `currency`
    currency TEXT NOT NULL DEFAULT 'CHF',
    fx_rate REAL NOT NULL DEFAULT 1,
    amount_chf REAL NOT NULL,
    gross_amount REAL,
    kind TEXT NOT NULL DEFAULT 'salary',
    note TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS statements (
    id INTEGER PRIMARY KEY,
    month TEXT NOT NULL,                   -- YYYY-MM the statement covers
    filename TEXT NOT NULL,
    stored_name TEXT NOT NULL,
    content_type TEXT,
    note TEXT,
    uploaded_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS recurring (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    amount REAL NOT NULL,
    currency TEXT NOT NULL DEFAULT 'CHF',
    category_id INTEGER REFERENCES categories(id) ON DELETE SET NULL,
    day_of_month INTEGER,
    start_month TEXT,                      -- YYYY-MM, null = always
    end_month TEXT,                        -- YYYY-MM, null = open ended
    active INTEGER NOT NULL DEFAULT 1,
    note TEXT
);
CREATE TABLE IF NOT EXISTS transactions (
    id INTEGER PRIMARY KEY,
    date TEXT NOT NULL,                    -- YYYY-MM-DD
    description TEXT NOT NULL,
    amount REAL NOT NULL,                  -- positive = money spent, negative = refund
    currency TEXT NOT NULL DEFAULT 'CHF',
    fx_rate REAL NOT NULL DEFAULT 1,
    amount_chf REAL NOT NULL,
    category_id INTEGER REFERENCES categories(id) ON DELETE SET NULL,
    person_id INTEGER REFERENCES people(id) ON DELETE SET NULL,
    recurring_id INTEGER REFERENCES recurring(id) ON DELETE SET NULL,
    statement_id INTEGER REFERENCES statements(id) ON DELETE SET NULL,
    account TEXT,
    note TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    -- set on a foreign-currency expense that has been pulled into a single CHF summary line
    -- ("UK expenses"); such rows are left out of CHF totals because the summary line counts them
    rollup_id INTEGER REFERENCES transactions(id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS idx_tx_date ON transactions(date);
CREATE TABLE IF NOT EXISTS fx_rates (
    currency TEXT NOT NULL,
    date TEXT NOT NULL,
    rate REAL NOT NULL,                    -- 1 unit of `currency` = `rate` CHF
    source TEXT NOT NULL DEFAULT 'manual',
    PRIMARY KEY (currency, date)
);
CREATE TABLE IF NOT EXISTS accounts (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    type TEXT NOT NULL DEFAULT 'cash',
    currency TEXT NOT NULL DEFAULT 'CHF',
    institution TEXT,
    person_id INTEGER REFERENCES people(id) ON DELETE SET NULL,
    active INTEGER NOT NULL DEFAULT 1,
    note TEXT
);
CREATE TABLE IF NOT EXISTS valuations (
    id INTEGER PRIMARY KEY,
    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    date TEXT NOT NULL,
    value REAL NOT NULL,
    fx_rate REAL NOT NULL DEFAULT 1,
    value_chf REAL NOT NULL,
    UNIQUE (account_id, date)
);
CREATE TABLE IF NOT EXISTS flows (
    id INTEGER PRIMARY KEY,
    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    date TEXT NOT NULL,
    amount REAL NOT NULL,                  -- positive = deposit, negative = withdrawal
    fx_rate REAL NOT NULL DEFAULT 1,
    amount_chf REAL NOT NULL,
    note TEXT
);
"""

DEFAULT_CATEGORIES = [
    "Housing", "Food", "Travel", "Transport", "Insurance", "Health",
    "Utilities", "Entertainment", "Shopping", "Subscriptions", "Other",
]


TABLES = ["people", "categories", "budgets", "income", "statements", "recurring", "transactions",
          "fx_rates", "accounts", "valuations", "flows"]

STORAGE = (os.environ.get("STORAGE") or ("dynamodb" if os.environ.get("DYNAMODB_TABLE") else "sqlite")).lower()

_store = None                    # DynamoStore when STORAGE == "dynamodb"
_mirror: sqlite3.Connection | None = None
_needs_reload = False
_lock = threading.Lock()


def _seed(conn: sqlite3.Connection) -> None:
    if conn.execute("SELECT COUNT(*) FROM people").fetchone()[0] == 0:
        conn.executemany("INSERT INTO people (name) VALUES (?)", [("Person 1",), ("Person 2",)])
    if conn.execute("SELECT COUNT(*) FROM categories").fetchone()[0] == 0:
        conn.executemany("INSERT INTO categories (name) VALUES (?)", [(c,) for c in DEFAULT_CATEGORIES])


# ------------------------------------------------------------------ SQLite file mode (local / offline)

def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=15, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _get_db_sqlite():
    conn = connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ------------------------------------------------------------------ DynamoDB mode
#
# DynamoDB is the system of record. Because the reports need joins and aggregations that DynamoDB
# cannot do, the rows are mirrored into an in-memory SQLite database: loaded once at startup, and
# every change a request makes is captured by triggers and written to DynamoDB before the request
# is acknowledged. If that write fails the request fails (503) and nothing changes.

def _key_expr(table: str, prefix: str) -> str:
    return f"{prefix}.currency || '#' || {prefix}.date" if table == "fx_rates" else f"{prefix}.id"


def _sk(table: str, key: str) -> str:
    return key if table == "fx_rates" else f"{int(key):010d}"


def _cast(value, sql_type: str):
    if isinstance(value, Decimal):
        return int(value) if sql_type == "INTEGER" else float(value)
    return value


def _build_mirror() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    for t in TABLES:
        cols = {r["name"]: r["type"] for r in conn.execute(f"PRAGMA table_info({t})")}
        for item in _store.load(t):
            row = {c: _cast(item[c], typ) for c, typ in cols.items() if item.get(c) is not None}
            conn.execute(f"INSERT INTO {t} ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})",
                         list(row.values()))
    conn.commit()
    ddl = ["CREATE TABLE _changes (tbl TEXT NOT NULL, key TEXT NOT NULL);"]
    for t in TABLES:
        for op, ref in (("INSERT", "NEW"), ("UPDATE", "NEW"), ("DELETE", "OLD")):
            ddl.append(f"CREATE TRIGGER _track_{t}_{op} AFTER {op} ON {t} BEGIN "
                       f"INSERT INTO _changes (tbl, key) VALUES ('{t}', {_key_expr(t, ref)}); END;")
    conn.executescript("\n".join(ddl))
    conn.execute("PRAGMA foreign_keys = ON")
    _seed(conn)
    _flush(conn)
    conn.commit()
    return conn


def _flush(conn: sqlite3.Connection) -> None:
    """Send every row touched in the current transaction to DynamoDB."""
    changes = conn.execute("SELECT DISTINCT tbl, key FROM _changes").fetchall()
    if not changes:
        return
    puts, deletes = [], []
    for c in changes:
        t, key = c["tbl"], c["key"]
        row = conn.execute(f"SELECT * FROM {t} WHERE {_key_expr(t, t)} = ?", (key,)).fetchone()
        if row:
            puts.append((t, _sk(t, key), dict(row)))
        else:
            deletes.append((t, _sk(t, key)))
    conn.execute("DELETE FROM _changes")
    _store.apply(puts, deletes)


def _get_db_dynamo():
    global _mirror, _needs_reload
    from fastapi import HTTPException
    from .dynamo import StoreError

    _lock.acquire()
    try:
        if _needs_reload:
            try:
                _mirror = _build_mirror()
                _needs_reload = False
            except StoreError as e:
                raise HTTPException(503, f"DynamoDB is unavailable: {e}")
        conn = _mirror
        try:
            yield conn
            _flush(conn)
            conn.commit()
        except StoreError as e:
            conn.rollback()
            _needs_reload = True          # re-read from DynamoDB before serving the next request
            raise HTTPException(503, f"Could not save to DynamoDB, nothing was changed: {e}")
        except Exception:
            conn.rollback()
            raise
    finally:
        _lock.release()


def reload_from_store() -> dict:
    """Discard the in-memory mirror and read everything from DynamoDB again."""
    global _mirror
    if STORAGE != "dynamodb":
        return {"reloaded": False, "storage": STORAGE}
    with _lock:
        _mirror = _build_mirror()
        return {"reloaded": True, "storage": STORAGE,
                "rows": {t: _mirror.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in TABLES}}


# ------------------------------------------------------------------ common entry points

def init_db() -> None:
    global _store, _mirror
    if STORAGE == "dynamodb":
        from .dynamo import DynamoStore
        _store = DynamoStore()
        _store.ensure_table()
        _mirror = _build_mirror()
        return
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    conn = connect()
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA)
        # columns added after the first release (an existing file keeps its old table definition)
        if "rollup_id" not in {r["name"] for r in conn.execute("PRAGMA table_info(transactions)")}:
            conn.execute("ALTER TABLE transactions ADD COLUMN rollup_id INTEGER "
                         "REFERENCES transactions(id) ON DELETE SET NULL")
        _seed(conn)
        conn.commit()
    finally:
        conn.close()


def get_db():
    """FastAPI dependency: a connection for one request, committed (and persisted) on success."""
    yield from (_get_db_dynamo() if STORAGE == "dynamodb" else _get_db_sqlite())


@contextmanager
def session():
    """The same connection handling as get_db, for code that runs outside a request (background jobs)."""
    yield from get_db()


def storage_info() -> dict:
    if STORAGE == "dynamodb" and _store:
        return {"storage": "dynamodb", "table": _store.table, "region": _store.region}
    return {"storage": STORAGE}


def save_file(name: str, data: bytes) -> None:
    if STORAGE == "dynamodb":
        _store.put_file(name, data)
    else:
        (UPLOAD_DIR / name).write_bytes(data)


def read_file(name: str) -> bytes | None:
    if STORAGE == "dynamodb":
        return _store.get_file(name)
    path = UPLOAD_DIR / name
    return path.read_bytes() if path.exists() else None


def delete_file(name: str) -> None:
    if STORAGE == "dynamodb":
        _store.delete_file(name)
    else:
        (UPLOAD_DIR / name).unlink(missing_ok=True)
