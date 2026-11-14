# Household Finance Tracker

A self-hosted finance tracker for two people: salaries, monthly expenses, budgets, recurring
charges, investments, and GBP spending — all reported in CHF. Built to be driven by an agent over
HTTP, with a dashboard for looking back over time.

## Run

1. Create an IAM user for the app with the policy under [Storage](#storage-dynamodb) and generate an access key for it.
2. Copy `.env.example` to `.env` and fill in the access key, secret and region.
3. Start it:

```bash
docker compose up -d --build
```

| | |
|---|---|
| Dashboard | http://localhost:1113 |
| API | http://localhost:1311/api |
| API docs (Swagger) | http://localhost:1311/docs |
| Agent guide | http://localhost:1311/api/agent-guide (source: [docs/AGENT_GUIDE.md](docs/AGENT_GUIDE.md)) |

## Storage: DynamoDB

All data, including uploaded bank screenshots, is stored in one DynamoDB table. The container
keeps nothing on disk, so it can be destroyed and recreated freely.

**IAM policy for the app's user** (replace region, account id and table name):

```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Action": [
      "dynamodb:DescribeTable",
      "dynamodb:CreateTable",
      "dynamodb:Query",
      "dynamodb:PutItem",
      "dynamodb:DeleteItem"
    ],
    "Resource": "arn:aws:dynamodb:eu-central-2:123456789012:table/household-finance"
  }]
}
```

`CreateTable` is only used on the very first start (on-demand billing, keys `pk` + `sk`, both
strings). If you would rather create the table yourself, do that, drop `CreateTable` from the
policy and set `DYNAMODB_CREATE_TABLE=false`.

**How it works.** DynamoDB is the system of record. Reports need joins and sums that DynamoDB
cannot do, so on start the app reads the table into memory and answers queries from there; every
change is written to DynamoDB before the request is acknowledged. If that write fails the request
returns `503` and nothing is changed.

**Things to know**

- Run **one** container per table. Two containers would not see each other's writes.
- If you edit items directly in the AWS console, call `POST /api/admin/reload` (or restart) so the app sees them.
- Turn on point-in-time recovery for the table in the AWS console — that is your backup.
- Table layout: `pk` is the kind of record (`transactions`, `income`, `accounts`, …) and `sk` its id;
  screenshots are stored in 300 KB chunks under `pk = file#<name>`.
- The access key lives only in `.env`, which is ignored by git and not copied into the image.

For offline use without AWS, set `STORAGE=sqlite` and mount a volume at `/data`; the app then
keeps a local SQLite file instead.

### Settings (environment variables, set in `.env`)

| Variable | Default | Meaning |
|---|---|---|
| `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` | — | Access key of the IAM user. Required. |
| `AWS_REGION` | — | Region of the table, e.g. `eu-central-2`. Required. |
| `DYNAMODB_TABLE` | `household-finance` | Table name. |
| `DYNAMODB_CREATE_TABLE` | `true` | Create the table on first start if it is missing. |
| `API_KEY` | empty | If set, every API call needs the `X-API-Key` header; the dashboard asks for it once. |
| `FX_AUTO_FETCH` | `true` | Fetch exchange rates from frankfurter.dev (ECB rates): the day's rate for GBP and any other foreign currency in use is stored automatically at start and every 6 hours, and missing past rates are looked up when needed. Only a currency and date are sent. A rate you set by hand for a day is never overwritten. |
| `DASHBOARD_PORT` / `API_PORT` | `1113` / `1311` | Ports inside the container. |

## Screens

- **Overview** — income, spending, left over, net worth; income vs expenses and net worth over time; spending by category; what to send to savings.
- **Salary** — both salaries by month and by year.
- **Expenses** — the month's transactions, category trend, uploaded bank screenshots. **Pull UK expenses** turns the month's GBP expenses into one CHF line at today's rate plus 10 % for transfer (Wise) fees.
- **Recurring** — rent, car payment, etc.: paid / not paid for the month.
- **Investments** — accounts, value over time, gain and return vs money paid in. **Statement** on an account lists every deposit, withdrawal (with its note) and recorded value.
- **GBP** — UK spending in pounds and CHF, exchange-rate history.
- **Settings** — names, categories, links for the agent.

There is no budgets screen. Budgets can still be set over the API (`PUT /api/budgets`); when a category
has one, the spending bars show it.

## Try it with demo data

Against a fresh, empty instance:

```bash
docker compose exec finance python scripts/seed_demo.py
```

Use a separate table for this (`DYNAMODB_TABLE=household-finance-demo`) and delete that table
afterwards; the demo data is written to DynamoDB like any other data.

## Develop without Docker

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements.txt pytest httpx "moto[dynamodb]"
.venv/Scripts/python -m app.main
.venv/Scripts/python -m pytest tests
```

Without `DYNAMODB_TABLE` or `STORAGE=dynamodb` set, the app runs on a local SQLite file in `./data`.
The tests run on SQLite by default; set `TEST_STORAGE=dynamodb` to run them against an in-process
DynamoDB mock (no AWS account needed).

Layout: `app/api.py` (all endpoints), `app/db.py` (schema, in-memory mirror, write-through),
`app/dynamo.py` (DynamoDB access), `app/fx.py` (currency conversion), `app/main.py` (server on
both ports), `app/static/` (dashboard).
