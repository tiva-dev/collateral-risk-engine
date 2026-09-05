from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from typing import Any

import httpx


def policy_payload() -> dict[str, Any]:
    return {
        "base_ltv": {"etf": 0.70, "listed_equity": 0.60},
        "portfolio_ltv_cap": 0.70,
        "risk_appetite": "balanced",
    }


def interest_policy_payload() -> dict[str, Any]:
    return {
        "quoted_interest_rate": 0.10,
        "rate_period": "yearly",
        "accrual_frequency": "daily",
        "payment_frequency": "at_maturity",
        "compounding": "simple",
        "day_count_convention": "actual_365",
        "term_days": 365,
    }


def holding_payload() -> dict[str, Any]:
    return {
        "asset_id": "SPY",
        "asset_type": "etf",
        "quantity": 100.0,
        "currency": "USD",
        "exchange": "NYSE",
        "provider_id": "SPY",
    }


def direct_market_data_payload(price: float, timestamp: str) -> dict[str, Any]:
    return {
        "asset_id": "SPY",
        "last_price": price,
        "bid": round(price - 0.10, 2),
        "ask": round(price + 0.10, 2),
        "average_daily_volume": 1_000_000,
        "average_dollar_volume": price * 1_000_000,
        "volatility_30d": 0.18,
        "volatility_90d": 0.20,
        "volatility_252d": 0.22,
        "max_drawdown_252d": 0.25,
        "max_gap_252d": 0.08,
        "timestamp": timestamp,
        "data_quality_score": 1.0,
        "order_book": {
            "bids": [{"price": round(price - 0.10, 2), "quantity": 10_000}],
            "asks": [{"price": round(price + 0.10, 2), "quantity": 10_000}],
        },
    }


def client_quote_payload(price: float, timestamp: str) -> dict[str, Any]:
    market_data = direct_market_data_payload(price, timestamp)
    return {
        "asset_id": "SPY",
        "symbol": "SPY",
        "exchange": "NYSE",
        "currency": "USD",
        "asset_type": "etf",
        "local_price": market_data["last_price"],
        "bid": market_data["bid"],
        "ask": market_data["ask"],
        "average_daily_volume": market_data["average_daily_volume"],
        "average_dollar_volume": market_data["average_dollar_volume"],
        "volatility_30d": market_data["volatility_30d"],
        "volatility_90d": market_data["volatility_90d"],
        "volatility_252d": market_data["volatility_252d"],
        "max_drawdown_252d": market_data["max_drawdown_252d"],
        "max_gap_252d": market_data["max_gap_252d"],
        "timestamp": timestamp,
        "data_quality_score": 1.0,
        "order_book": market_data["order_book"],
    }


def _call(
    client: httpx.Client,
    method: str,
    path: str,
    *,
    json_body: dict[str, Any] | None = None,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    response = client.request(method, path, json=json_body, params=params)
    if response.is_error:
        raise RuntimeError(
            f"{method} {path} returned {response.status_code}: {response.text}"
        )
    payload = response.json()
    print(f"\n{method} {path} -> {response.status_code}")
    print(json.dumps(payload, indent=2))
    return payload


def run_walkthrough(
    base_url: str,
    *,
    api_key: str | None = None,
    account_ref: str | None = None,
    cleanup: bool = False,
) -> None:
    timestamp = datetime.now(UTC).isoformat()
    resolved_account_ref = account_ref or (
        f"manual-client-{datetime.now(UTC).strftime('%Y%m%d%H%M%S')}"
    )
    headers = {"X-CRI-API-Key": api_key} if api_key else {}
    with httpx.Client(
        base_url=base_url,
        headers=headers,
        timeout=30.0,
        trust_env=False,
    ) as client:
        _call(client, "GET", "/health")
        origination = _call(
            client,
            "POST",
            "/credit/originate",
            json_body={
                "account_ref": resolved_account_ref,
                "policy": policy_payload(),
                "holdings": [holding_payload()],
                "market_data": {
                    "SPY": direct_market_data_payload(100.0, timestamp)
                },
                "loan_terms": interest_policy_payload(),
            },
        )
        approved_limit = float(origination["result"]["approved_credit_limit"])
        draw_amount = round(approved_limit * 0.50, 2)
        _call(
            client,
            "POST",
            "/monitoring/accounts",
            json_body={
                "account_ref": resolved_account_ref,
                "holdings": [holding_payload()],
                "loan": {"principal": 0.0, "currency": "USD"},
                "loan_currency": "USD",
                "policy": policy_payload(),
                "interest_policy": interest_policy_payload(),
                "data_mode": "client_supplied",
                "client_supplied_quotes": {
                    "SPY": client_quote_payload(100.0, timestamp)
                },
            },
        )
        _call(
            client,
            "POST",
            f"/monitoring/accounts/{resolved_account_ref}/draws",
            json_body={"amount": draw_amount, "draw_reference": "manual-draw-1"},
        )
        shocked_timestamp = datetime.now(UTC).isoformat()
        _call(
            client,
            "POST",
            "/monitoring/market-data/update",
            json_body={
                "quote_updates": {
                    "SPY": client_quote_payload(35.0, shocked_timestamp)
                },
                "source": "manual_client_walkthrough",
                "trigger_tick": True,
            },
        )
        _call(
            client,
            "GET",
            "/monitoring/events",
            params={"account_ref": resolved_account_ref},
        )
        _call(
            client,
            "POST",
            f"/monitoring/accounts/{resolved_account_ref}/repayments",
            json_body={"amount": 100.0, "repayment_reference": "manual-repay-1"},
        )
        _call(
            client,
            "POST",
            f"/monitoring/accounts/{resolved_account_ref}/liquidation/fills",
            json_body={
                "execution_reference": "manual-fill-1",
                "fills": [
                    {
                        "asset_id": "SPY",
                        "quantity": 1.0,
                        "execution_price": 34.90,
                        "fees": 0.10,
                    }
                ],
            },
        )
        _call(
            client,
            "GET",
            f"/monitoring/accounts/{resolved_account_ref}",
        )
        if cleanup:
            _call(
                client,
                "DELETE",
                f"/monitoring/accounts/{resolved_account_ref}",
            )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run a lender-style CRI API lifecycle against a local server."
    )
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--api-key")
    parser.add_argument("--account-ref")
    parser.add_argument("--cleanup", action="store_true")
    arguments = parser.parse_args()
    run_walkthrough(
        arguments.base_url,
        api_key=arguments.api_key,
        account_ref=arguments.account_ref,
        cleanup=arguments.cleanup,
    )


if __name__ == "__main__":
    main()
