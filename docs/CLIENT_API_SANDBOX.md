# Client API sandbox

This sandbox lets a lender exercise the CRI lifecycle through real HTTP
requests without provider credentials or production infrastructure. It uses
client supplied market data and a separate local SQLite database.

## Start the API

From the repository root:

```bash
python -m pip install -r requirements.txt
CRI_STATE_DB_PATH=./data/runtime/client-sandbox.sqlite3 \
  python -m uvicorn app.main:app --reload
```

On Windows PowerShell:

```powershell
$env:CRI_STATE_DB_PATH = ".\data\runtime\client-sandbox.sqlite3"
python -m uvicorn app.main:app --reload
```

Open <http://127.0.0.1:8000/docs>. Select an endpoint, choose **Try it out**,
edit the request body, and select **Execute**. Every public endpoint is
available in this page.

When `CRI_API_KEYS` is configured, the documentation remains accessible.
Select **Authorize** and enter the client key. Swagger sends it as
`X-CRI-API-Key` with each API call.

## Run the complete lender journey

Keep the API running, then open a second terminal:

```bash
python -m app.examples.client_api_walkthrough
```

The walkthrough performs these calls and prints every response:

1. health check;
2. credit origination for a one year loan;
3. monitoring account registration;
4. idempotent loan draw notification;
5. a severe client supplied market price update and immediate monitoring tick;
6. monitoring event retrieval;
7. idempotent repayment notification;
8. liquidation fill feedback;
9. final account retrieval.

Use `--cleanup` to remove the walkthrough account when it finishes. Use
`--api-key YOUR_KEY` when authentication is enabled. The script creates a
unique account reference and current UTC timestamps automatically.

## Manual client sequence

The recommended order in Swagger is:

| Order | Endpoint | Client action |
|---:|---|---|
| 1 | `POST /credit/originate` | Submit policy, portfolio, prices, and loan terms; save `approved_credit_limit` |
| 2 | `POST /monitoring/accounts` | Register the collateral and a zero balance loan |
| 3 | `POST /monitoring/accounts/{account_ref}/draws` | Notify CRI after the lender disburses an approved draw |
| 4 | `POST /monitoring/market-data/update` | Send a newer quote and set `trigger_tick` to `true` |
| 5 | `GET /monitoring/events` | Inspect warnings, margin calls, and liquidation advice |
| 6 | `POST /monitoring/accounts/{account_ref}/repayments` | Notify CRI of a customer repayment |
| 7 | `POST /monitoring/accounts/{account_ref}/liquidation/fills` | Report actual quantity, price, and fees after execution |
| 8 | `GET /monitoring/accounts/{account_ref}` | Confirm remaining collateral, debt, and margin state |

Every draw, repayment, loan update, and liquidation execution needs a unique
reference. Reusing the same reference is intentionally idempotent: the balance
must not change twice.

The lender supplies policy limits and business terms. The public API does not
accept a liquidation participation rate. CRI derives safe liquidity from
market observations and retains an internal absolute safety ceiling.

## Automated contract test

The repository verifies the same HTTP journey with an isolated in memory
monitoring service:

```bash
python -m pytest -q app/tests/test_client_api_journey.py
```

This test covers origination, registration, draws, duplicate references,
market updates, monitoring events, repayments, liquidation fills, Swagger, and
API key authorization.
