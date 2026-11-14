"""Currency conversion to CHF. CHF is the base currency for everything."""
import json
import os
import sqlite3
import urllib.request

from fastapi import HTTPException

BASE = "CHF"
AUTO_FETCH = os.environ.get("FX_AUTO_FETCH", "true").lower() in ("1", "true", "yes")


def _fetch_rate(currency: str, date: str) -> float | None:
    """Best-effort ECB reference rate from frankfurter.dev (no key, no personal data sent)."""
    url = f"https://api.frankfurter.dev/v1/{date}?base={currency}&symbols={BASE}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "finance-tracker"})
        with urllib.request.urlopen(req, timeout=4) as resp:
            return float(json.load(resp)["rates"][BASE])
    except Exception:
        return None


def resolve_rate(db: sqlite3.Connection, currency: str, date: str, explicit: float | None = None) -> float:
    """Return how many CHF one unit of `currency` was worth on `date`.

    Order: explicit rate -> stored rate for that date -> auto-fetched rate (then stored)
    -> closest stored rate before the date -> closest stored rate after it -> 422.
    """
    if currency == BASE:
        return 1.0
    if explicit is not None:
        if explicit <= 0:
            raise HTTPException(422, "fx_rate must be positive")
        return float(explicit)
    row = db.execute("SELECT rate FROM fx_rates WHERE currency=? AND date=?", (currency, date)).fetchone()
    if row:
        return row["rate"]
    if AUTO_FETCH:
        rate = _fetch_rate(currency, date)
        if rate:
            db.execute(
                "INSERT OR REPLACE INTO fx_rates (currency, date, rate, source) VALUES (?,?,?,'auto')",
                (currency, date, rate),
            )
            return rate
    row = db.execute(
        "SELECT rate FROM fx_rates WHERE currency=? AND date<=? ORDER BY date DESC LIMIT 1", (currency, date)
    ).fetchone() or db.execute(
        "SELECT rate FROM fx_rates WHERE currency=? ORDER BY date ASC LIMIT 1", (currency,)
    ).fetchone()
    if row:
        return row["rate"]
    raise HTTPException(
        422,
        f"No {currency}->CHF exchange rate available for {date}. Pass fx_rate in the request "
        f"or store one first with PUT /api/fx.",
    )


def to_chf(db: sqlite3.Connection, amount: float, currency: str, date: str, explicit: float | None = None):
    """Return (fx_rate, amount_in_chf)."""
    rate = resolve_rate(db, currency, date, explicit)
    return rate, round(amount * rate, 2)
