"""Learning Agents in isolated processes with no outbound network (demonstration).

Each agent runs as its own OS process, started inside a fresh, empty network
namespace (``unshare --net``): it has no route to any host, so the only channel
out of the process is the pipe to the coordinator, which carries the protocol
messages only. The institution's data is created inside the agent process and
never crosses the pipe. The process also gets a memory and CPU-time limit.

In production the same boundary is a separate machine or VPC in the
institution's environment with egress restricted to the coordinator endpoint;
this module demonstrates the protocol across real process and network
isolation, not that deployment.
"""

from __future__ import annotations

import json
import resource
import shutil
import socket
import subprocess
import sys
from dataclasses import replace
from typing import Any

ISOLATION_CMD = ["unshare", "--net", "--map-root-user"]


def isolation_available() -> bool:
    if shutil.which("unshare") is None:
        return False
    try:
        return subprocess.run([*ISOLATION_CMD, "true"], capture_output=True, timeout=10, check=False).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _enc(x: Any) -> Any:
    if isinstance(x, bytes):
        return {"__b": x.hex()}
    if isinstance(x, dict):
        return {k: _enc(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_enc(v) for v in x]
    return x


def _dec(x: Any) -> Any:
    if isinstance(x, dict):
        if set(x) == {"__b"}:
            return bytes.fromhex(x["__b"])
        return {k: _dec(v) for k, v in x.items()}
    if isinstance(x, list):
        return [_dec(v) for v in x]
    return x


def _network_reachable() -> bool:
    for host in ("1.1.1.1", "8.8.8.8"):
        try:
            with socket.create_connection((host, 443), timeout=2):
                return True
        except OSError:
            continue
    return False


def serve() -> None:
    """Agent process: one JSON request per line on stdin, one response per line on stdout."""
    from app.federation.agent import AgentRefusal, Consent, LearningAgent
    from app.federation.simulate import build, institutions
    resource.setrlimit(resource.RLIMIT_AS, (2 * 1024 ** 3, 2 * 1024 ** 3))
    resource.setrlimit(resource.RLIMIT_CPU, (600, 600))
    agent: LearningAgent | None = None
    for line in sys.stdin:
        req = json.loads(line)
        try:
            if req["method"] == "init":
                a = req["args"]
                insts = institutions(a["count"], a["heterogeneity"], a["seed"])
                envs, _ = build(a["seed"], insts)          # this institution's data exists only here
                consent = Consent(frozenset(a["algorithms"]), frozenset(a["schemas"]))
                agent = LearningAgent(a["institution"], envs[a["institution"]], replace(consent),
                                      bytes.fromhex(a["coordinator_public"]))
                res: Any = {"identity": agent.identity.public.hex(), "network_reachable": _network_reachable(),
                            "interfaces": sorted(n for _, n in socket.if_nameindex()),
                            "rows": agent.env.rows("margin_call_outcome")}
            else:
                assert agent is not None
                if req["method"] not in ("registration", "advertise", "share_keys", "receive_shares",
                                         "masked_contribution", "unmask"):
                    raise AgentRefusal("method not exposed by the agent")
                res = getattr(agent, req["method"])(*_dec(req["args"]))
            sys.stdout.write(json.dumps({"ok": _enc(res)}) + "\n")
        except AgentRefusal as e:
            sys.stdout.write(json.dumps({"refused": str(e)}) + "\n")
        sys.stdout.flush()


class AgentProcess:
    """Coordinator-side handle: forwards the protocol calls to an isolated agent process."""

    def __init__(self, institution: str, coordinator_public: bytes, seed: int, count: int = 6,
                 heterogeneity: float = 1.0, isolate: bool = True):
        cmd = [*(ISOLATION_CMD if isolate else []), sys.executable, "-m", "app.federation.isolated"]
        self.institution = institution
        self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        info = self._call("init", {"institution": institution, "seed": seed, "count": count,
                                   "heterogeneity": heterogeneity, "coordinator_public": coordinator_public.hex(),
                                   "algorithms": ["cure_logistic_v1"], "schemas": ["margin_call_outcome"]},
                          raw=True)
        self.public = bytes.fromhex(info["identity"])
        self.network_reachable = bool(info["network_reachable"])
        self.interfaces = list(info["interfaces"])
        self.rows = int(info["rows"])

    def _call(self, method: str, args: Any, raw: bool = False) -> Any:
        from app.federation.agent import AgentRefusal
        assert self.proc.stdin is not None and self.proc.stdout is not None
        self.proc.stdin.write(json.dumps({"method": method, "args": args if raw else _enc(list(args))}) + "\n")
        self.proc.stdin.flush()
        resp = json.loads(self.proc.stdout.readline())
        if "refused" in resp:
            raise AgentRefusal(resp["refused"])
        return _dec(resp["ok"])

    # the LearningAgent protocol surface used by the coordinator
    def registration(self) -> dict[str, Any]:
        return dict(self._call("registration", ()))

    def advertise(self, package: dict[str, Any], signature: bytes) -> dict[str, Any]:
        return dict(self._call("advertise", (package, signature)))

    def share_keys(self, roster: list[dict[str, Any]], identities: dict[str, bytes], threshold: int) -> dict[str, bytes]:
        return dict(self._call("share_keys", (roster, identities, threshold)))

    def receive_shares(self, sender: str, blob: bytes) -> None:
        self._call("receive_shares", (sender, blob))

    def masked_contribution(self) -> dict[str, Any]:
        return dict(self._call("masked_contribution", ()))

    def unmask(self, survivors: list[str], dropped: list[str]) -> dict[str, Any]:
        return dict(self._call("unmask", (survivors, dropped)))

    def close(self) -> None:
        if self.proc.stdin:
            self.proc.stdin.close()
        self.proc.wait(timeout=30)


def isolated_federation(seed: int = 7, rounds: int = 3, isolate: bool = True) -> dict[str, Any]:
    """Runs cure_logistic_v1 rounds with every agent in its own isolated process."""
    import tempfile
    from pathlib import Path

    from app.federation.coordinator import Coordinator
    from app.federation.simulate import institutions
    from app.learning.governance import ModelRegistry
    coord = Coordinator(ModelRegistry(Path(tempfile.mkdtemp()) / "iso.jsonl"), min_participants=3)
    names = [i.name for i in institutions(6, 1.0, seed)]
    procs = {n: AgentProcess(n, coord.identity.public, seed, isolate=isolate) for n in names}
    try:
        for n, p in procs.items():
            coord.enroll(n, p.public)
            coord.register(p.registration())
        ref = [0.1, 4.0, 11.0, 5.0, 0.6, 0.25, 0.65]
        refsd = [0.07, 2.0, 1.6, 3.5, 0.8, 0.43, 0.18]
        params = {"epochs": 3, "lr": 0.2, "l2": 1e-3, "prox_mu": 0.1, "clip": 1.0, "noise_multiplier": 0.0,
                  "weight_cap": 1500}
        hist = [coord.run_round(procs, "cure_logistic_v1", params, ref, refsd)   # type: ignore[arg-type]
                for _ in range(rounds)]
        return {"isolated": isolate, "agents": len(procs),
                "network_reachable_from_any_agent": any(p.network_reachable for p in procs.values()),
                "agent_interfaces": sorted({i for p in procs.values() for i in p.interfaces}),
                "rounds": [{"round": h["round"], "survivors": len(h["survivors"])} for h in hist],
                "model": coord.models["cure_logistic_v1"][-1]["model"]}
    finally:
        for p in procs.values():
            p.close()


if __name__ == "__main__":
    serve()
