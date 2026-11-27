"""Provider-independent LLM interface for CRI Analyst.

Internal message format (provider-neutral):
    {"role": "user", "content": str}
    {"role": "assistant", "content": str | None, "tool_calls": [{"id", "name", "args"}]}
    {"role": "tool", "tool_call_id": str, "name": str, "content": dict}

Providers:
* ``VertexGemini``      Gemini on Vertex AI (generateContent, function calling)
* ``VertexClaude``      Claude on Vertex AI (Anthropic Messages via rawPredict)
* ``OpenAICompatible``  open-weight models (Gemma, Qwen) behind vLLM / Ollama /
                        any OpenAI-compatible chat-completions server
* ``ScriptedPlanner``   deterministic offline planner for tests only

Cloud authentication uses the runtime service account (metadata server) or
``CRI_GCP_ACCESS_TOKEN``; no API keys are stored in code. Prices used for the
cost budget come from configuration and are estimates, not invoices.
"""

from __future__ import annotations

import json
import os
import urllib.request
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class LLMResponse:
    text: str | None
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    model: str = ""
    raw_stop: str = ""


class LLMProvider(Protocol):
    @property
    def name(self) -> str: ...
    @property
    def model(self) -> str: ...
    @property
    def external(self) -> bool: ...      # data leaves Tiva's GCP boundary

    def complete(self, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> LLMResponse: ...
    def cost_usd(self, r: LLMResponse) -> float: ...


def _post(url: str, body: dict[str, Any], headers: dict[str, str], timeout: int = 90) -> dict[str, Any]:
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json", **headers})
    with urllib.request.urlopen(req, timeout=timeout) as r:  # nosec B310 - fixed Vertex AI / metadata-server / configured endpoint
        return json.loads(r.read())


def gcp_access_token() -> str:
    tok = os.environ.get("CRI_GCP_ACCESS_TOKEN")
    if tok:
        return tok
    req = urllib.request.Request(
        "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token",
        headers={"Metadata-Flavor": "Google"})
    with urllib.request.urlopen(req, timeout=5) as r:  # nosec B310 - fixed Vertex AI / metadata-server / configured endpoint
        return json.loads(r.read())["access_token"]


_GEMINI_KEYS = {"type", "description", "properties", "required", "items", "enum", "minimum", "maximum", "nullable"}


def _gemini_schema(s: Any) -> Any:
    """Gemini accepts an OpenAPI subset; validation stays with the gateway."""
    if isinstance(s, dict):
        out = {k: _gemini_schema(v) for k, v in s.items() if k in _GEMINI_KEYS}
        if "properties" in s:
            out["properties"] = {k: _gemini_schema(v) for k, v in s["properties"].items()}
        if out.get("type") == "object" and not out.get("properties"):
            out.pop("properties", None)
        return out
    if isinstance(s, list):
        return [_gemini_schema(x) for x in s]
    return s


@dataclass
class _Priced:
    price_in_per_m: float = float(os.environ.get("CRI_LLM_PRICE_IN_PER_M", "1.0"))
    price_out_per_m: float = float(os.environ.get("CRI_LLM_PRICE_OUT_PER_M", "5.0"))

    def cost_usd(self, r: LLMResponse) -> float:
        return r.input_tokens / 1e6 * self.price_in_per_m + r.output_tokens / 1e6 * self.price_out_per_m


@dataclass
class VertexGemini(_Priced):
    project: str = ""
    region: str = "europe-west1"
    model: str = "gemini-2.5-flash"
    name: str = "vertex-gemini"
    external: bool = False      # stays within Google Cloud under the GCP data terms
    post: Callable[..., dict[str, Any]] = _post

    def complete(self, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> LLMResponse:
        contents: list[dict[str, Any]] = []
        for m in messages:
            if m["role"] == "user":
                contents.append({"role": "user", "parts": [{"text": m["content"]}]})
            elif m["role"] == "assistant":
                parts: list[dict[str, Any]] = [{"text": m["content"]}] if m.get("content") else []
                parts += [{"functionCall": {"name": c["name"], "args": c["args"]}} for c in m.get("tool_calls", [])]
                contents.append({"role": "model", "parts": parts})
            elif m["role"] == "tool":
                contents.append({"role": "user", "parts": [{"functionResponse": {
                    "name": m["name"], "response": {"content": m["content"]}}}]})
        body = {"systemInstruction": {"parts": [{"text": system}]}, "contents": contents,
                "tools": [{"functionDeclarations": [{"name": t["name"], "description": t["description"],
                                                     "parameters": _gemini_schema(t["parameters"])} for t in tools]}],
                "generationConfig": {"temperature": 0.1, "maxOutputTokens": 2048}}
        host = "aiplatform.googleapis.com" if self.region == "global" else f"{self.region}-aiplatform.googleapis.com"
        url = (f"https://{host}/v1/projects/{self.project}/locations/{self.region}"
               f"/publishers/google/models/{self.model}:generateContent")
        d = self.post(url, body, {"Authorization": f"Bearer {gcp_access_token()}"})
        cand = (d.get("candidates") or [{}])[0]
        parts = cand.get("content", {}).get("parts", [])
        text = "".join(p.get("text", "") for p in parts if "text" in p) or None
        calls = [{"id": uuid.uuid4().hex[:8], "name": p["functionCall"]["name"], "args": p["functionCall"].get("args", {})}
                 for p in parts if "functionCall" in p]
        u = d.get("usageMetadata", {})
        return LLMResponse(text, calls, int(u.get("promptTokenCount", 0)), int(u.get("candidatesTokenCount", 0)),
                           self.model, cand.get("finishReason", ""))


def _anthropic_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for m in messages:
        if m["role"] == "user":
            out.append({"role": "user", "content": m["content"]})
        elif m["role"] == "assistant":
            blocks: list[dict[str, Any]] = [{"type": "text", "text": m["content"]}] if m.get("content") else []
            blocks += [{"type": "tool_use", "id": c["id"], "name": c["name"], "input": c["args"]}
                       for c in m.get("tool_calls", [])]
            out.append({"role": "assistant", "content": blocks})
        elif m["role"] == "tool":
            block = {"type": "tool_result", "tool_use_id": m["tool_call_id"], "content": json.dumps(m["content"])}
            if out and out[-1]["role"] == "user" and isinstance(out[-1]["content"], list):
                out[-1]["content"].append(block)
            else:
                out.append({"role": "user", "content": [block]})
    return out


@dataclass
class VertexClaude(_Priced):
    project: str = ""
    region: str = "europe-west1"
    model: str = "claude-sonnet-4-5"
    name: str = "vertex-claude"
    external: bool = False
    post: Callable[..., dict[str, Any]] = _post

    def complete(self, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> LLMResponse:
        body = {"anthropic_version": "vertex-2023-10-16", "system": system, "max_tokens": 2048, "temperature": 0.1,
                "messages": _anthropic_messages(messages),
                "tools": [{"name": t["name"], "description": t["description"], "input_schema": t["parameters"]}
                          for t in tools]}
        host = "aiplatform.googleapis.com" if self.region == "global" else f"{self.region}-aiplatform.googleapis.com"
        url = (f"https://{host}/v1/projects/{self.project}/locations/{self.region}"
               f"/publishers/anthropic/models/{self.model}:rawPredict")
        d = self.post(url, body, {"Authorization": f"Bearer {gcp_access_token()}"})
        text = "".join(b.get("text", "") for b in d.get("content", []) if b.get("type") == "text") or None
        calls = [{"id": b["id"], "name": b["name"], "args": b.get("input", {})}
                 for b in d.get("content", []) if b.get("type") == "tool_use"]
        u = d.get("usage", {})
        return LLMResponse(text, calls, int(u.get("input_tokens", 0)), int(u.get("output_tokens", 0)), self.model,
                           d.get("stop_reason", ""))


@dataclass
class OpenAICompatible(_Priced):
    base_url: str = "http://localhost:8000/v1"
    model: str = "google/gemma-3-12b-it"
    api_key_env: str = "CRI_OPENWEIGHT_API_KEY"
    name: str = "openai-compatible"
    external: bool = False      # self-hosted inside Tiva's environment
    post: Callable[..., dict[str, Any]] = _post

    def complete(self, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> LLMResponse:
        msgs: list[dict[str, Any]] = [{"role": "system", "content": system}]
        for m in messages:
            if m["role"] == "user":
                msgs.append({"role": "user", "content": m["content"]})
            elif m["role"] == "assistant":
                msgs.append({"role": "assistant", "content": m.get("content") or "",
                             "tool_calls": [{"id": c["id"], "type": "function",
                                             "function": {"name": c["name"], "arguments": json.dumps(c["args"])}}
                                            for c in m.get("tool_calls", [])] or None})
            elif m["role"] == "tool":
                msgs.append({"role": "tool", "tool_call_id": m["tool_call_id"], "content": json.dumps(m["content"])})
        body = {"model": self.model, "messages": msgs, "temperature": 0.1,
                "tools": [{"type": "function", "function": t} for t in tools]}
        key = os.environ.get(self.api_key_env, "")
        d = self.post(f"{self.base_url}/chat/completions", body, {"Authorization": f"Bearer {key}"} if key else {})
        msg = d["choices"][0]["message"]
        calls = [{"id": c["id"], "name": c["function"]["name"], "args": json.loads(c["function"]["arguments"] or "{}")}
                 for c in msg.get("tool_calls") or []]
        u = d.get("usage", {})
        return LLMResponse(msg.get("content"), calls, int(u.get("prompt_tokens", 0)), int(u.get("completion_tokens", 0)),
                           self.model, d["choices"][0].get("finish_reason", ""))


@dataclass
class ScriptedPlanner(_Priced):
    """Deterministic planner for tests: a fixed sequence of tool calls followed
    by an answer built only from tool outputs. Not a product demonstration."""

    script: list[Callable[[list[dict[str, Any]]], LLMResponse]] = field(default_factory=list)
    name: str = "scripted"
    model: str = "scripted-planner"
    external: bool = False
    price_in_per_m: float = 0.0
    price_out_per_m: float = 0.0

    def complete(self, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> LLMResponse:
        step = sum(1 for m in messages if m["role"] == "assistant")
        return self.script[min(step, len(self.script) - 1)](messages)


def gcp_project() -> str:
    p = os.environ.get("CRI_LLM_PROJECT") or os.environ.get("GOOGLE_CLOUD_PROJECT")
    if p:
        return p
    req = urllib.request.Request("http://metadata.google.internal/computeMetadata/v1/project/project-id",
                                 headers={"Metadata-Flavor": "Google"})
    with urllib.request.urlopen(req, timeout=5) as r:  # nosec B310 - fixed Vertex AI / metadata-server / configured endpoint
        return r.read().decode()


@dataclass
class FirstAvailable:
    """Try candidate model ids in order (model availability differs by region
    and changes over time); the first that answers is kept for the session."""

    candidates: list[Any]
    chosen: Any = None
    errors: list[str] = field(default_factory=list)
    attempted: list[str] = field(default_factory=list)

    @property
    def name(self) -> str:
        return (self.chosen or self.candidates[0]).name

    @property
    def model(self) -> str:
        return (self.chosen or self.candidates[0]).model

    @property
    def external(self) -> bool:
        return any(c.external for c in self.candidates)

    def complete(self, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> LLMResponse:
        if self.chosen is not None:
            return self.chosen.complete(system, messages, tools)
        import urllib.error
        for c in self.candidates:
            label = f"{c.model}@{getattr(c, 'region', '')}"
            if label not in self.attempted:
                self.attempted.append(label)
            try:
                r = c.complete(system, messages, tools)
                self.chosen = c
                return r
            except urllib.error.HTTPError as e:
                try:
                    detail = json.loads(e.read() or b"{}").get("error", {}).get("message", "")[:160]
                except (ValueError, AttributeError):
                    detail = ""
                self.errors.append(f"{c.model}@{getattr(c, 'region', '')}: HTTP {e.code} {detail}".strip())
                if e.code not in (400, 404):
                    raise
        raise RuntimeError("no candidate model available: " + "; ".join(self.errors))

    def cost_usd(self, r: LLMResponse) -> float:
        return (self.chosen or self.candidates[0]).cost_usd(r)


_NOT_TEXT = ("image", "embedding", "tts", "live", "audio", "vision", "veo", "imagen", "native", "computer-use",
             "robotics", "lyria", "chirp", "thinking-exp")


def _rank_gemini(names: list[tuple[str, str]]) -> list[str]:
    """Order discovered Gemini text models: newest generation first (Google
    restricts older generations for projects without prior usage), then Flash
    before Pro (cost), then GA before preview."""
    def version(m: str) -> tuple[float, ...]:
        import re as _re
        v = _re.search(r"gemini-(\d+(?:\.\d+)?)", m)
        return (float(v.group(1)),) if v else (0.0,)
    rows = []
    for name, stage in names:
        m = name.rsplit("/", 1)[-1]
        if not m.startswith("gemini") or any(x in m for x in _NOT_TEXT):
            continue
        ga = 0 if stage in ("GA", "") else 1
        tier = 0 if "flash" in m and "lite" not in m else (1 if "lite" in m else 2)
        rows.append(((tuple(-x for x in version(m)), tier, ga), m))
    return [m for _, m in sorted(set(rows))]


def discover_gemini_models(project: str, region: str,
                           get: Callable[[str, dict[str, str]], dict[str, Any]] | None = None) -> list[str]:
    """List Google publisher models available to the project (Model Garden)."""
    def _get(url: str, headers: dict[str, str]) -> dict[str, Any]:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=20) as r:  # nosec B310 - fixed Vertex AI endpoint
            out: dict[str, Any] = json.loads(r.read())
            return out
    fetch = get or _get
    host = "aiplatform.googleapis.com" if region == "global" else f"{region}-aiplatform.googleapis.com"
    names: list[tuple[str, str]] = []
    token = ""
    for _ in range(10):
        import urllib.parse
        q = {"pageSize": "200", **({"pageToken": token} if token else {})}
        url = f"https://{host}/v1beta1/publishers/google/models?" + urllib.parse.urlencode(q)
        d = fetch(url, {"Authorization": f"Bearer {gcp_access_token()}", "x-goog-user-project": project})
        names += [(m.get("name", ""), m.get("launchStage", "")) for m in d.get("publisherModels", [])]
        token = d.get("nextPageToken", "")
        if not token:
            break
    return _rank_gemini(names)


def provider_from_env() -> LLMProvider:
    kind = os.environ.get("CRI_LLM_PROVIDER", "vertex-gemini")
    project = gcp_project() if kind.startswith("vertex") else ""
    region = os.environ.get("CRI_LLM_REGION", "europe-west1")
    if kind == "vertex-gemini":
        configured = os.environ.get("CRI_LLM_MODEL", "")
        if configured:
            models = configured.split(",")
        else:
            models = []
            for loc in (region, "global"):
                try:
                    models = discover_gemini_models(project, loc)[:10]
                except Exception:  # noqa: BLE001 - discovery is best effort; fall back to known ids
                    models = []
                if models:
                    break
            models = models or ["gemini-2.5-flash", "gemini-2.0-flash-001", "gemini-3-flash-preview"]
        regions = [region, "global"] if region != "global" else ["global"]
        return FirstAvailable([VertexGemini(project=project, region=r, model=m.strip()) for m in models for r in regions])
    if kind == "vertex-claude":
        return VertexClaude(project=project, region=region, model=os.environ.get("CRI_LLM_MODEL", "claude-sonnet-4-5"))
    if kind == "openai-compatible":
        return OpenAICompatible(base_url=os.environ["CRI_LLM_BASE_URL"], model=os.environ["CRI_LLM_MODEL"])
    raise ValueError(f"unknown CRI_LLM_PROVIDER {kind!r}")
