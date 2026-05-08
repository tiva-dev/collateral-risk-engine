from __future__ import annotations

from dataclasses import dataclass

from app.core.models import OrderBook
from app.risk.math_utils import clamp


@dataclass(frozen=True)
class OrderBookExecutionEstimate:
    requested_quantity: float
    filled_quantity: float
    proceeds: float
    average_price: float
    residual_quantity: float
    order_book_slippage_rate: float


def estimate_market_sell_from_order_book(
    quantity: float,
    last_price: float,
    order_book: OrderBook | None,
) -> OrderBookExecutionEstimate | None:
    if order_book is None or not order_book.bids or quantity <= 0 or last_price <= 0:
        return None

    remaining = quantity
    proceeds = 0.0
    filled = 0.0
    for level in sorted(order_book.bids, key=lambda x: x.price, reverse=True):
        if remaining <= 0:
            break
        if level.price <= 0 or level.quantity <= 0:
            continue
        take = min(remaining, level.quantity)
        proceeds += take * level.price
        filled += take
        remaining -= take

    avg_price = proceeds / filled if filled > 0 else 0.0
    notional_at_last = max(1e-9, filled * last_price)
    slippage = clamp(1.0 - proceeds / notional_at_last, 0.0, 1.0) if filled > 0 else 1.0

    return OrderBookExecutionEstimate(
        requested_quantity=quantity,
        filled_quantity=filled,
        proceeds=proceeds,
        average_price=avg_price,
        residual_quantity=max(0.0, remaining),
        order_book_slippage_rate=slippage,
    )
