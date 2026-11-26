"""Browser check of the Control Centre session flow against a stub API.

Serves app/platform/console under /console/ and answers the API with fixed
JSON for two test keys. Drives headless Chrome (desktop and mobile viewports)
through: failed sign-in, sign-in, login hidden and scroll at top, session
restore on reload, logout, expired-session restore.

    python infra/scripts/console_browser_check.py
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[2] / "app" / "platform" / "console"
KEYS = {"cri_test_sandbox": "sandbox", "cri_test_admin": "admin"}
REVOKED: set[str] = set()


class Stub(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, code, body):
        b = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path.startswith("/console"):
            name = path[len("/console/"):] or "index.html"
            f = ROOT / name
            if not f.is_file():
                return self._json(404, {"detail": "not found"})
            ctype = {"html": "text/html", "js": "text/javascript", "css": "text/css"}[f.suffix[1:]]
            b = f.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)
            return
        key = (self.headers.get("Authorization") or "").removeprefix("Bearer ")
        role = KEYS.get(key)
        if role is None or key in REVOKED:
            return self._json(401, {"detail": "invalid API key"})
        if path == "/platform/whoami":
            return self._json(200, {"tenant_id": "tiva-sandbox" if role == "sandbox" else "tiva-operator",
                                    "role": role, "environment": "test"})
        if path == "/platform/controls":
            return self._json(200, {"controls": {"lending_enabled": False, "liquidation_execution_enabled": False}})
        if path == "/monitoring/accounts":
            return self._json(200, {"accounts": [{"account_ref": "acc-1"}]})
        if path.startswith("/shadow/accounts/"):
            th = {"initial_capacity": 600.0, "initial_capacity_uncapped": 700.0, "early_warning": 800.0,
                  "margin_call": 850.0, "forced_recovery": 900.0, "restoration_target": 700.0, "client_ceiling": 600.0}
            return self._json(200, {"evaluations": [
                {"model_id": "baseline", "risk_state": "safe"},
                {"model_id": "candidate", "model_version": "candidate-test", "risk_state": "safe",
                 "collateral_value": 1000.0, "loan_balance": 500.0, "evaluated_at": "2026-11-27T00:00:00Z",
                 "explanation": {"thresholds": th, "threshold_ltv": {k: v / 1000 for k, v in th.items()},
                                 "threshold_reasoning": {k: f"reason for {k}" for k in th}, "data_status": "ok",
                                 "instruments": [{"key": "NYSE:AAA:USD:LISTED_EQUITY", "sessions": 400}],
                                 "assets": [{"asset_id": "AAA", "market_value": 1000.0}], "parameters": {},
                                 "reasons": ["test"]}}]})
        if path == "/admin/tenants":
            return self._json(200, {"tenants": [{"tenant_id": "tiva-sandbox", "kind": "sandbox"}]})
        return self._json(200, {})


def check(page, base: str, label: str) -> list[str]:
    out = []

    def ok(cond: bool, what: str) -> None:
        out.append(f"{'PASS' if cond else 'FAIL'} [{label}] {what}")

    def visible(sel: str) -> bool:
        return page.evaluate(f"getComputedStyle(document.querySelector('{sel}')).display") != "none"

    page.goto(base + "/console/")
    ok(visible("#login") and not visible("#shell"), "fresh load shows only the login")
    page.fill("#login-key", "cri_wrong")
    page.click("#login-form button")
    page.wait_for_function("document.querySelector('#login-error').textContent.length > 0")
    ok(visible("#login") and not visible("#shell"), "failed sign-in keeps the login, no dashboard")
    ok("invalid" in page.text_content("#login-error"), "failed sign-in shows the error")
    page.evaluate("window.scrollTo(0, 400)")
    page.fill("#login-key", "cri_test_sandbox")
    page.click("#login-form button")
    page.wait_for_function("getComputedStyle(document.querySelector('#shell')).display !== 'none'")
    ok(not visible("#login"), "after sign-in the login is fully hidden")
    ok(page.evaluate("document.querySelector('#login').getBoundingClientRect().height") == 0,
       "login occupies no space")
    ok(page.evaluate("window.scrollY") == 0, "scroll position reset to top")
    ok(page.evaluate("document.querySelector('#shell').getBoundingClientRect().top") <= 1,
       "dashboard starts at the top of the viewport")
    ok(page.input_value("#login-key") == "", "key field cleared")
    page.reload()
    page.wait_for_function("getComputedStyle(document.querySelector('#shell')).display !== 'none'")
    ok(not visible("#login"), "session restored on reload without the login")
    page.click("#logout")
    ok(visible("#login") and not visible("#shell"), "logout returns to the login only")
    ok(page.evaluate("sessionStorage.getItem('cri_key')") is None, "logout clears the stored key")
    page.fill("#login-key", "cri_test_admin")
    page.click("#login-form button")
    page.wait_for_function("getComputedStyle(document.querySelector('#shell')).display !== 'none'")
    ok(visible("#tenant-select"), "admin sign-in shows the tenant selector")
    page.get_by_text("Candidate engine", exact=True).first.click()
    page.wait_for_function("document.querySelector('#main').textContent.includes('reason for margin_call')")
    txt = page.text_content("#main")
    ok(all(w in txt for w in ("Initial lending capacity", "Early warning", "Margin call", "Forced recovery",
                              "Restoration target")), "candidate view shows all five thresholds with reasoning")
    REVOKED.add("cri_test_admin")
    page.reload()
    page.wait_for_function("getComputedStyle(document.querySelector('#login')).display !== 'none'")
    ok(not visible("#shell") and "expired" in page.text_content("#login-error"),
       "revoked stored key returns to the login with a message")
    REVOKED.discard("cri_test_admin")
    return out


def main() -> None:
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Stub)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_port}"
    results = []
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path="/usr/bin/google-chrome", args=["--no-sandbox"])
        for label, kw in (("desktop 1440x900", {"viewport": {"width": 1440, "height": 900}}),
                          ("mobile 390x844", {"viewport": {"width": 390, "height": 844}, "is_mobile": True,
                                              "has_touch": True, "device_scale_factor": 3})):
            ctx = browser.new_context(**kw)
            page = ctx.new_page()
            results += check(page, base, label)
            page.screenshot(path=f"/tmp/console_{label.split()[0]}.png")
            ctx.close()
        browser.close()
    srv.shutdown()
    print("\n".join(results))
    if any(r.startswith("FAIL") for r in results):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
