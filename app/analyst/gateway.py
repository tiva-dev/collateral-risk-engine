"""Tenant-scoped tool gateway: the only path from an LLM to CRI.

Guarantees, each enforced here (not by prompting):

* **Tenant isolation** - the gateway is bound to one tenant's ``BookSource`` at
  construction; tool arguments cannot name a tenant (schemas forbid extra
  properties) and account references outside the tenant fail.
* **Read-only** - only tools declared ``mutates=False`` can be registered.
* **Strict validation** - JSON Schema validation of every call; unknown tools
  are refused.
* **Budgets** - per-question limits on tool calls and LLM calls, per-tenant
  rate limit (requests per minute) and daily token and cost ceilings.
* **Data minimisation** - when the provider is external, account references
  are pseudonymised (HMAC with a per-session key) before results leave CRI and
  mapped back in the final answer; borrower identifiers never enter results.
* **Prompt-injection containment** - tool results are returned as JSON data
  inside a typed envelope; free-text fields are stripped of control characters,
  truncated, and instruction-like content is neutralised. Even if an injection
  succeeded, the reachable capability is read-only and tenant-bound.
* **Audit** - every call is recorded with argument and result hashes, timing,
  outcome and the release fingerprint.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import jsonschema  # type: ignore[import-untyped]

from app.analyst.tools import Tool, Toolset, release_info, simulation_info

_INJECTION = re.compile(r"(ignore (all |any )?(previous|prior|above) instructions|disregard .{0,40}instructions|"
                        r"you are now|system prompt|developer message|<\s*/?\s*(system|assistant|tool)\s*>|"
                        r"(now |please )?(call|invoke) the [a-z_]+ tool|approve (this|the) loan|"
                        r"(execute|start|trigger) (a |the )?liquidation)", re.IGNORECASE)
_REF_KEYS = {"account_ref"}


class GatewayError(Exception):
    pass


class BudgetExceeded(GatewayError):
    pass


@dataclass
class Limits:
    max_tool_calls_per_question: int = 12
    max_llm_calls_per_question: int = 8
    requests_per_minute: int = 20
    daily_token_budget: int = 2_000_000
    daily_cost_budget_usd: float = 5.0
    max_string: int = 400


class _TenantMeter:
    """Process-local meter; the platform persists the same counters in the DB."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.requests: dict[str, deque[float]] = defaultdict(deque)
        self.tokens: dict[tuple[str, str], int] = defaultdict(int)
        self.cost: dict[tuple[str, str], float] = defaultdict(float)


METER = _TenantMeter()


@dataclass
class Session:
    tenant_id: str
    principal: str
    external_provider: bool
    key: bytes = field(default_factory=lambda: secrets.token_bytes(16))
    tool_calls: int = 0
    llm_calls: int = 0
    pseudonyms: dict[str, str] = field(default_factory=dict)

    def pseudo(self, ref: str) -> str:
        p = "acct_" + hmac.new(self.key, ref.encode(), hashlib.sha256).hexdigest()[:8]
        self.pseudonyms[p] = ref
        return p

    def real(self, ref: str) -> str:
        return self.pseudonyms.get(ref, ref)


class ToolGateway:
    def __init__(self, toolset: Toolset, principal: str, *, external_provider: bool, limits: Limits | None = None,
                 audit_path: str | Path | None = None, meter: _TenantMeter = METER):
        self.toolset = toolset
        self.tenant_id = toolset.book.tenant_id
        self.limits = limits or Limits()
        self.meter = meter
        self.session = Session(self.tenant_id, principal, external_provider)
        self.audit_path = Path(audit_path) if audit_path else None
        self.audit: list[dict[str, Any]] = []
        self._tools: dict[str, Tool] = {}
        for t in toolset.tools().values():
            if t.mutates:
                raise GatewayError(f"refusing to register mutating tool {t.name}")
            self._tools[t.name] = t

    # ------------------------------------------------------------ schema

    def tool_specs(self) -> list[dict[str, Any]]:
        return [{"name": t.name, "description": t.description, "parameters": t.parameters}
                for t in self._tools.values()]

    # ----------------------------------------------------------- budgets

    def admit_request(self) -> None:
        now = time.time()
        with self.meter.lock:
            q = self.meter.requests[self.tenant_id]
            while q and now - q[0] > 60:
                q.popleft()
            if len(q) >= self.limits.requests_per_minute:
                raise BudgetExceeded("rate limit: too many Analyst requests this minute")
            q.append(now)

    def charge_llm(self, tokens: int, cost_usd: float) -> None:
        day = datetime.now(UTC).date().isoformat()
        self.session.llm_calls += 1
        if self.session.llm_calls > self.limits.max_llm_calls_per_question:
            raise BudgetExceeded("LLM call limit for this question reached")
        with self.meter.lock:
            self.meter.tokens[(self.tenant_id, day)] += tokens
            self.meter.cost[(self.tenant_id, day)] += cost_usd
            if self.meter.tokens[(self.tenant_id, day)] > self.limits.daily_token_budget:
                raise BudgetExceeded("daily token budget for this tenant exhausted")
            if self.meter.cost[(self.tenant_id, day)] > self.limits.daily_cost_budget_usd:
                raise BudgetExceeded("daily LLM cost budget for this tenant exhausted")

    # -------------------------------------------------------------- call

    def call(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        t0 = time.time()
        rec: dict[str, Any] = {"at": datetime.now(UTC).isoformat(), "tenant": self.tenant_id,
                               "principal": self.session.principal, "tool": name,
                               "args_sha256": hashlib.sha256(json.dumps(args, sort_keys=True, default=str).encode()).hexdigest()[:16]}
        try:
            if name not in self._tools:
                raise GatewayError(f"unknown tool {name!r}")
            self.session.tool_calls += 1
            if self.session.tool_calls > self.limits.max_tool_calls_per_question:
                raise BudgetExceeded("tool call limit for this question reached")
            if not isinstance(args, dict):
                raise GatewayError("arguments must be an object")
            tool = self._tools[name]
            jsonschema.validate(args, tool.parameters)
            real_args = self._unpseudo(args)
            result = tool.fn(**real_args)
            safe = self._sanitise(self._pseudo(result))
            envelope = {"type": "cri_tool_result", "tool": name, "provenance": {**release_info(),
                        "simulation": simulation_info(self.toolset.n_paths),
                        "tenant_scoped": True, "computed_at": datetime.now(UTC).isoformat()},
                        "data": safe}
            rec.update(outcome="ok", result_sha256=hashlib.sha256(
                json.dumps(safe, sort_keys=True, default=str).encode()).hexdigest()[:16])
            return envelope
        except jsonschema.ValidationError as e:
            rec.update(outcome="invalid_arguments", error=e.message[:200])
            return {"type": "cri_tool_error", "tool": name, "error": f"invalid arguments: {e.message[:200]}"}
        except KeyError as e:
            rec.update(outcome="not_found", error=str(e)[:200])
            return {"type": "cri_tool_error", "tool": name, "error": "not found in this tenant's book"}
        except BudgetExceeded:
            rec.update(outcome="budget_exceeded")
            raise
        except GatewayError as e:
            rec.update(outcome="refused", error=str(e)[:200])
            return {"type": "cri_tool_error", "tool": name, "error": str(e)[:200]}
        finally:
            rec["ms"] = int((time.time() - t0) * 1000)
            self.audit.append(rec)
            if self.audit_path:
                self.audit_path.parent.mkdir(parents=True, exist_ok=True)
                with self.audit_path.open("a") as f:
                    f.write(json.dumps(rec) + "\n")

    # ----------------------------------------------------- minimisation

    def _pseudo(self, obj: Any, key: str | None = None) -> Any:
        if not self.session.external_provider:
            return obj
        if isinstance(obj, dict):
            return {k: self._pseudo(v, k) for k, v in obj.items()}
        if isinstance(obj, list):
            return [self._pseudo(v, key) for v in obj]
        if key in _REF_KEYS and isinstance(obj, str):
            return self.session.pseudo(obj)
        return obj

    def _unpseudo(self, obj: Any) -> Any:
        if isinstance(obj, dict):
            return {k: self._unpseudo(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [self._unpseudo(v) for v in obj]
        if isinstance(obj, str):
            return self.session.real(obj)
        return obj

    def pseudonymise_text(self, text: str) -> str:
        """Replace this tenant's account references in free text (e.g. the user's
        question) before it is sent to an external provider."""
        if not self.session.external_provider:
            return text
        for ref in sorted(self.toolset.book.account_refs(), key=len, reverse=True):
            if ref in text:
                text = re.sub(rf"(?<![A-Za-z0-9_-]){re.escape(ref)}(?![A-Za-z0-9_-])", self.session.pseudo(ref), text)
        return text

    def restore_refs(self, text: str) -> str:
        for p, r in self.session.pseudonyms.items():
            text = text.replace(p, r)
        return text

    def _sanitise(self, obj: Any) -> Any:
        if isinstance(obj, dict):
            return {k: self._sanitise(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [self._sanitise(v) for v in obj]
        if isinstance(obj, str):
            s = "".join(ch for ch in obj if ch.isprintable())[: self.limits.max_string]
            return _INJECTION.sub("[removed]", s)
        return obj
