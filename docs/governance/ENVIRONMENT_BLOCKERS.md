# Access and Environment Blockers

Recorded by the engineering agent on 25 Nov 2026.

| Item | Blocker | Effect | Needed from Tiva |
|---|---|---|---|
| GitHub write access | Resolved 25 Nov 2026: repository collaborator access plus the Claude GitHub App on this repository | Branches pushed; PRs #22 and #23 opened | Remove access when the engagement ends |
| GitHub Actions logs and artifacts | Logs readable through the API; artifact ZIP downloads are blocked by the agent's network policy | Cause of run 34000523667 confirmed from logs; v0.6.2 ZIP (run 30455533912) has expired and is not listed | Check for any offline copy of the v0.6.2 ZIP |
| Python packages | Agent workspace has no package index access; fastapi and httpx are missing | 7 test modules (API, monitoring, portfolio-action, v0.7.0) could not be run locally; Bandit, pip-audit and build not run locally | CI run on this branch |
| Provider data (Alpaca, NGNMarket, Alpha Vantage) | **Partly resolved 25 Nov 2026.** Provider calls run in GitHub Actions with repository secrets; NGNMarket Hobby plan active (no more 403). Remaining gaps are history depth and source disagreement, quantified in `DATA_INVENTORY.md` | Preliminary real-data experiments run in Actions from snapshot-verified artifacts | US 2018-2020 history (Alpaca SIP or another source: expenditure decision); NGN FX before 2023-10-18; choice of NGN reference rate |
| Commit `ffe8d02`, August work | Not in any GitHub ref; no local folder connected | Cannot reconcile | Connect the folder or push the commit |
| Whitepaper v1.0 (30 Jul 2026) | Not in the repository | Claims not checked against code | Share the document |
