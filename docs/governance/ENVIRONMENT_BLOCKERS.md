# Access and Environment Blockers

Recorded by the engineering agent on 25 Nov 2026.

| Item | Blocker | Effect | Needed from Tiva |
|---|---|---|---|
| GitHub write access | Push credentials were not granted to the agent session | Phase 0 changes are delivered as a git bundle and patches, not pushed | Push the bundle, or grant access |
| GitHub Actions logs and artifacts | Require signed-in access | Cause of run 34000523667 not confirmed; v0.6.2 ZIP not re-verified | Download artifact of run 34000523667 before ~5 Dec 2026; check for any copy of run 30455533912's ZIP |
| Python packages | Agent workspace has no package index access; fastapi and httpx are missing | 7 test modules (API, monitoring, portfolio-action, v0.7.0) could not be run locally; Bandit, pip-audit and build not run locally | CI run on this branch |
| Provider data (Alpaca, NGNMarket, Alpha Vantage) | No credentials; provider hosts blocked from the workspace | Methodology experiments use synthetic data only; no empirical calibration | Provider credentials in a controlled environment, or exported normalized caches |
| Commit `ffe8d02`, August work | Not in any GitHub ref; no local folder connected | Cannot reconcile | Connect the folder or push the commit |
| Whitepaper v1.0 (30 Jul 2026) | Not in the repository | Claims not checked against code | Share the document |
