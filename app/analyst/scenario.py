"""Structured stress scenarios: the only form in which a natural-language stress
request reaches the engine.

The LLM (or a person) produces a ``Scenario``; it is validated against a strict
schema and applied to the engine's *inputs* (current prices, recent volumes,
settlement delay). The frozen RC1 engine then evaluates the stressed inputs
exactly as it evaluates live ones, so every stressed number is an engine output.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any

SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "name": {"type": "string", "maxLength": 120},
        "price_shocks": {
            "type": "array", "maxItems": 20,
            "items": {
                "type": "object", "additionalProperties": False,
                "required": ["change"],
                "properties": {
                    "sector": {"type": "string", "maxLength": 40},
                    "market": {"type": "string", "maxLength": 20},
                    "instrument": {"type": "string", "maxLength": 40},
                    "asset_type": {"type": "string", "maxLength": 30},
                    "change": {"type": "number", "minimum": -0.95, "maximum": 1.0},
                },
            },
        },
        "volume_change": {"type": "number", "minimum": -0.95, "maximum": 5.0},
        "volume_sessions": {"type": "integer", "minimum": 1, "maximum": 60},
        "settlement_delay_sessions": {"type": "integer", "minimum": 0, "maximum": 10},
    },
}


@dataclass(frozen=True)
class PriceShock:
    change: float                     # -0.30 = 30% decline
    sector: str | None = None
    market: str | None = None
    instrument: str | None = None
    asset_type: str | None = None

    def matches(self, h: Any, sector_of: dict[str, str]) -> bool:
        if h.asset_type.value == "cash":
            return False
        if self.instrument and h.asset_id.upper() != self.instrument.upper():
            return False
        if self.market and h.exchange.upper() != self.market.upper():
            return False
        if self.asset_type and h.asset_type.value != self.asset_type.lower():
            return False
        return not (self.sector and sector_of.get(h.asset_id.upper(), "").lower() != self.sector.lower())


@dataclass(frozen=True)
class Scenario:
    name: str = "scenario"
    price_shocks: tuple[PriceShock, ...] = ()
    volume_change: float = 0.0        # -0.50 = volumes fall by half
    volume_sessions: int = 20         # recent sessions whose volumes are scaled
    settlement_delay_sessions: int = 0
    notes: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Scenario:
        import jsonschema  # type: ignore[import-untyped]

        jsonschema.validate(d, SCHEMA)
        return cls(name=d.get("name", "scenario"),
                   price_shocks=tuple(PriceShock(**s) for s in d.get("price_shocks", [])),
                   volume_change=float(d.get("volume_change", 0.0)),
                   volume_sessions=int(d.get("volume_sessions", 20)),
                   settlement_delay_sessions=int(d.get("settlement_delay_sessions", 0)))

    def describe(self) -> dict[str, Any]:
        return {"name": self.name,
                "price_shocks": [{k: v for k, v in s.__dict__.items() if v is not None} for s in self.price_shocks],
                "volume_change": self.volume_change, "volume_sessions": self.volume_sessions,
                "settlement_delay_sessions": self.settlement_delay_sessions,
                "method": "instantaneous shock to current prices of matched holdings; recent volumes scaled; "
                          "extra settlement sessions added to the execution policy; RC1 re-evaluated on the "
                          "stressed inputs"}


def apply(view: Any, sc: Scenario, sector_of: dict[str, str]) -> tuple[Any, list[str]]:
    """Return a stressed copy of an account view and the list of shocked holdings."""
    from app.candidate.engine import data_key

    md, bars, hit = dict(view.market_data), {k: list(v) for k, v in view.bars.items()}, []
    for h in view.holdings:
        shock = next((s for s in sc.price_shocks if s.matches(h, sector_of)), None)
        if shock is None:
            continue
        m = md.get(h.stable_key)
        if m is None:
            continue
        f = 1.0 + shock.change
        md[h.stable_key] = replace(m, last_price=m.last_price * f,
                                   bid=None if m.bid is None else m.bid * f,
                                   ask=None if m.ask is None else m.ask * f)
        hit.append(h.asset_id)
    if sc.volume_change:
        f = 1.0 + sc.volume_change
        for h in view.holdings:
            k = data_key(h)
            if bars.get(k):
                n = min(sc.volume_sessions, len(bars[k]))
                bars[k] = bars[k][:-n] + [{**b, "volume": b["volume"] * f} for b in bars[k][-n:]]
    ex = view.execution_policy
    if sc.settlement_delay_sessions:
        ex = replace(ex, settlement_delay_observations=ex.settlement_delay_observations + sc.settlement_delay_sessions)
    return replace(view, market_data=md, bars=bars, execution_policy=ex), hit
