# Household Finance Tracker — Agent Guide

You are operating a personal finance tracker for a two-person household in Switzerland.
Everything a human can do in the dashboard you can do over this HTTP API. This guide is also
served live at `GET /api/agent-guide`; the machine-readable schema is at `/openapi.json` and
interactive docs at `/docs`.

## Connection

| | |
|---|---|
| Base URL | `http://<host>:1311/api` |
| Format | JSON in, JSON out (`Content-Type: application/json`), except the file upload (multipart) |
| Auth | None by default. If the server was started with `API_KEY`, send header `X-API-Key: <key>` on every call. `GET /api/health` tells you (`auth_required`). |
| Dashboard (for humans) | `http://<host>:1113` |

Use `127.0.0.1` rather than `localhost` when calling from the same Windows machine (IPv6 lookup makes `localhost` slow).

## Rules of the data model

- **CHF is the base currency.** Every money row stores the original `amount` + `currency` and the
  converted `amount_chf`. All totals and reports are in CHF (fields ending `_chf`).
- **Foreign currency (GBP).** Send `"currency": "GBP"` with the amount in pounds. The CHF value is
  worked out from, in order: the `fx_rate` you pass (CHF per 1 GBP) → a stored rate for that date →
  an automatically fetched ECB rate → the nearest stored rate. If none exists you get `422`; then
  either pass `fx_rate` or store one with `PUT /api/fx`. If the bank shows the CHF amount actually
  charged, pass `fx_rate = chf_charged / gbp_amount` so the numbers match the bank exactly.
- **Dates** are `YYYY-MM-DD`; **months** are `YYYY-MM`. A transaction belongs to the month of its `date`.
- **Expense sign:** positive `amount` = money spent; negative = refund.
- **Names instead of ids.** Wherever `category_id` or `person_id` is accepted you may send
  `category` / `person` with the name (case-insensitive). Unknown names return `422` listing the valid ones.
- **Errors:** `404` unknown id, `409` conflict (duplicate name, already paid), `422` invalid input —
  the `detail` field says what to fix. Bulk inserts are all-or-nothing.
- **`503`** means the data store (DynamoDB) could not be reached. Nothing was saved; wait and retry
  the same call. Do not work around it.
- **Never invent data.** If a screenshot is unreadable or a figure is missing, ask the human.

## The monthly close (your main job)

Run this once a month for the month that just ended (example: `2026-10`).

1. **Know the reference data** — `GET /people`, `GET /categories`, `GET /recurring`.
2. **Archive the bank screenshot** (optional but recommended):
   `POST /statements` multipart with `file=@screenshot.png` and `month=2026-10` → keep the returned `id`.
3. **Read the screenshot yourself** (the app does no OCR) and build one object per transaction.
   Choose the best-fitting category; leave it out if truly unsure (it will show as *Uncategorised*).
4. **Recurring charges first.** For each line that is one of the standing charges (rent, car…),
   call `POST /recurring/{id}/pay` with `{"month": "2026-10", "date": "...", "amount": <actual>}`
   and **do not** also include that line in the bulk upload (it would be counted twice).
5. **Load everything else** — `POST /transactions/bulk`:
   ```json
   {"statement_id": 12, "transactions": [
     {"date": "2026-10-03", "description": "Migros", "amount": 84.20, "category": "Food"},
     {"date": "2026-10-07", "description": "Amazon UK", "amount": 35.99, "currency": "GBP", "category": "Shopping"}
   ]}
   ```
   Retrying is safe: rows that already existed (same date, amount, currency, description) are skipped
   and listed in `skipped_duplicates`.
6. **Record both salaries** — `POST /income` once per person:
   `{"person": "<name>", "date": "2026-10-25", "amount": 7450, "gross_amount": 9100}`.
   Use `"kind": "bonus"` or `"13th_salary"` for extras.
7. **Update investment accounts** — for each account `POST /accounts/{id}/valuations`
   `{"date": "2026-10-31", "value": 41250.00}` (value in the account's own currency).
8. **Get the answer** — `GET /reports/monthly?month=2026-10`. It returns, in one response:
   - `income.by_person`, `income.missing_for` (people with no income recorded — chase this)
   - `expenses.total_chf`, `expenses.by_category` (with `budget_chf`, `remaining_chf`, `over_budget`)
   - `recurring.items` (each charge: `paid`, `paid_amount`, `difference`) and `recurring.unpaid`
   - `expenses.foreign_currency` (GBP total and its CHF value)
   - `savings.available_to_save_chf` = income − recorded expenses − recurring charges not yet paid
   - `savings.already_transferred_chf` and `savings.remaining_to_transfer_chf`
9. **Report to the humans**: what each recurring charge cost, which budgets were over/under,
   total spent, and "send CHF X to savings".
10. **After the transfer is made**, record it: `POST /accounts/{id}/flows`
    `{"date": "2026-11-02", "amount": 2500, "note": "October savings"}`. Flows are what let the app
    tell "we paid more in" apart from "the investments grew".

## Endpoint reference

All paths are relative to `/api`.

### Meta
| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | Liveness; `auth_required` flag; `storage` (`dynamodb` or `sqlite`) |
| GET | `/agent-guide` | This document |
| POST | `/admin/reload` | Re-read all data from DynamoDB. Only needed after someone edited the table outside this app |

### People
| Method | Path | Body / query | Purpose |
|---|---|---|---|
| GET | `/people` | | List household members |
| POST | `/people` | `name` | Add a person |
| PATCH | `/people/{id}` | `name?`, `active?` | Rename / deactivate |

### Income (salary)
| Method | Path | Body / query | Purpose |
|---|---|---|---|
| GET | `/income` | `?year=&month=&person_id=` | List payments |
| POST | `/income` | `person_id` or `person`, `date`, `amount` (net), `gross_amount?`, `currency?`, `fx_rate?`, `kind?` (`salary`\|`bonus`\|`13th_salary`\|`other`), `note?` | Record a payment |
| PATCH | `/income/{id}` | any of the above | Correct a payment |
| DELETE | `/income/{id}` | | Remove |
| GET | `/income/summary` | `?year=` | Per person: 12 monthly totals, by kind, year total; household total |
| GET | `/income/yearly` | | Totals per person for every year |

### Categories and budgets
| Method | Path | Body / query | Purpose |
|---|---|---|---|
| GET | `/categories` | | List (includes archived) |
| POST | `/categories` | `name` | Create |
| PATCH | `/categories/{id}` | `name?`, `archived?` | Rename / archive |
| DELETE | `/categories/{id}` | | Delete (transactions become uncategorised; prefer archive) |
| GET | `/budgets` | | Every budget row (history) |
| PUT | `/budgets` | `category_id` or `category`, `amount` (CHF/month, `0` removes), `valid_from?` (`YYYY-MM`, default this month) | Set a budget from a month onwards |
| DELETE | `/budgets/{id}` | | Remove one budget row |
| GET | `/budgets/status` | `?month=` | Budget vs spent per category |
| GET | `/budgets/history` | `?months=12&end=` | Same, for each of the last N months |

### Statements (bank screenshots)
| Method | Path | Body / query | Purpose |
|---|---|---|---|
| GET | `/statements` | `?month=` | List uploads |
| POST | `/statements` | multipart: `file` (image or PDF, ≤25 MB), `month`, `note?` | Archive a screenshot |
| GET | `/statements/{id}/file` | | Download the original |
| DELETE | `/statements/{id}` | | Delete the file (transactions are kept) |

```bash
curl -F "file=@oct.png" -F "month=2026-10" http://127.0.0.1:1311/api/statements
```

### Transactions (expenses)
| Method | Path | Body / query | Purpose |
|---|---|---|---|
| GET | `/transactions` | `?month=&date_from=&date_to=&category_id=&uncategorised=true&currency=&recurring_id=&statement_id=&q=&limit=` | List / search |
| POST | `/transactions` | `date`, `description`, `amount`, `currency?`, `fx_rate?`, `category_id` or `category`, `person_id` or `person`, `recurring_id?`, `statement_id?`, `account?`, `note?` | Record one expense |
| POST | `/transactions/bulk` | `transactions: [...]`, `statement_id?`, `skip_duplicates?` (default true) | Record many (all-or-nothing) |
| PATCH | `/transactions/{id}` | any field | Correct / re-categorise |
| DELETE | `/transactions/{id}` | | Remove |

### Recurring charges
| Method | Path | Body / query | Purpose |
|---|---|---|---|
| GET | `/recurring` | | List definitions |
| POST | `/recurring` | `name`, `amount`, `currency?`, `category_id` or `category`, `day_of_month?`, `start_month?`, `end_month?`, `note?` | Define a monthly charge |
| PATCH | `/recurring/{id}` | any of the above, `active?` | Change it (set `end_month` to stop it and keep history) |
| DELETE | `/recurring/{id}` | | Delete the definition (past payments stay) |
| GET | `/recurring/status` | `?month=` | Paid / unpaid per charge, totals |
| POST | `/recurring/{id}/pay` | `month?`, `date?`, `amount?`, `fx_rate?`, `statement_id?`, `note?` | Mark paid (creates the transaction). `409` if already paid that month |

If the amount of a charge changes permanently (rent increase), `PATCH` the definition as well.

### Exchange rates
| Method | Path | Body / query | Purpose |
|---|---|---|---|
| GET | `/fx` | `?currency=GBP` | Stored rates |
| PUT | `/fx` | `currency`, `date`, `rate` (CHF per 1 unit) | Store a rate |
| GET | `/fx/rate` | `?currency=GBP&date=` | The rate the app would use |

### Investments
Account `type`: `cash`, `savings`, `stocks`, `pension`, `crypto`, `property`, `other`.

| Method | Path | Body / query | Purpose |
|---|---|---|---|
| GET | `/accounts` | | Accounts with latest value, net paid in, gain, return % |
| POST | `/accounts` | `name`, `type?`, `currency?`, `institution?`, `person_id?`, `note?` | Create |
| PATCH | `/accounts/{id}` | `name?`, `type?`, `institution?`, `person_id?`, `active?`, `note?` | Edit / close (`active=false`) |
| DELETE | `/accounts/{id}` | | Delete with all history (prefer closing) |
| GET | `/accounts/{id}/valuations` | | Value history |
| POST | `/accounts/{id}/valuations` | `date`, `value`, `fx_rate?` | Record value on a date (same date overwrites) |
| DELETE | `/valuations/{id}` | | Remove a value |
| GET | `/accounts/{id}/flows` | | Deposits / withdrawals |
| POST | `/accounts/{id}/flows` | `date`, `amount` (+ in, − out), `fx_rate?`, `note?` | Record money moved in or out |
| DELETE | `/flows/{id}` | | Remove a flow |
| GET | `/investments/summary` | | Net worth, split by type, total gain |
| GET | `/investments/history` | `?months=24&end=` | Month-end value per account and cumulative paid in |

Performance is a simple return: `gain = latest value − net flows`, `return_pct = gain / net flows`.
It is only shown for accounts that have at least one flow, so for a stock account record the
opening balance as a first deposit.

### Reports
| Method | Path | Query | Purpose |
|---|---|---|---|
| GET | `/reports/monthly` | `?month=` | The month close (see step 8) |
| GET | `/reports/overview` | `?months=12&end=` | Income, expenses, net, savings rate per month + net worth history |
| GET | `/reports/categories` | `?months=12&end=` | Spending per category per month |
| GET | `/reports/yearly` | `?year=` | Year totals, by month, by category, income per person |
| GET | `/reports/foreign` | `?currency=GBP&months=12&end=` | GBP spending per month in GBP and CHF |

## First-time setup checklist

1. `PATCH /people/1` and `/people/2` with the real names.
2. `POST /recurring` for rent, car payment, insurance, etc.
3. `PUT /budgets` for Food, Travel, and any other budgeted category.
4. `POST /accounts` for emergency cash, stocks, pension…; then a first `flows` entry (opening
   balance) and a first `valuations` entry for each.
5. `PUT /fx` with a current GBP rate if the server has no internet access.
