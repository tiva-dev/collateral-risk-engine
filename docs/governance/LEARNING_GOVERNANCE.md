# Learning governance: approvals, persistence and data boundaries

Status: 28 Nov 2026. Implements review findings 6 and 7 for CRI Intelligence, Optimizer and Federation.

## Approval and rollback

- Every learned model or parameter update enters the registry (`app/learning/governance.py`) at the research stage.
- **Approval:** reaching approved requires a signed token (`app/learning/approvals.py`) from an approver registered in the `ApproverRegistry`, holding the `model_approval` role. The token must:
  - be bound to the exact model version and the decision;
  - carry evidence references;
  - be valid for at most 7 days.
- **Rollback:** requires a `model_rollback` token for the version being retired.
- **Identity:** the recorded approver is the identity verified from the signature. A caller's actor string carries no authority.
- **Blocked actors:** pipeline, federation, agent and Analyst actors are refused outright.
- **No registry, no approvals:** without an approver registry, approvals and rollbacks are impossible.
- **Key custody:** approver private keys never reside on the platform. In production they are hardware keys, or a signer bound to the institution's SSO identity. The platform stores only public keys and roles.

## Persistence: current state and what must change before institutional deployment

| Record | Current store (research and staging) | Required before institutional deployment |
|---|---|---|
| Model registry events | Append-only JSONL with a process and file lock | Cloud SQL table, insert-only (no UPDATE or DELETE grants), with a hash chain over events; nightly export to a versioned GCS bucket |
| Analyst audit records | JSONL per gateway | The same database, tenant-partitioned, insert-only |
| Analyst usage budgets and rate limits | Process-local counters | Database counters with row locks, shared by all API instances |
| Federation round audit | Coordinator memory and registry events | The same database, plus a signed round transcript stored in GCS |
| Experiment tracking | JSONL | The same database, or Vertex AI Experiments; decided when real training jobs run in staging |

The local stores are adequate for research and single-process demonstrations only. Moving them is a precondition for any institutional pilot.

## Data boundary (RC1 locked period)

- **Physical cut.** Research datasets are built and cut at the examined-through date (2026-11-25) by the publishing workflow. Their manifest records the boundary and every file's checksum.
- **Access control.** In GCP the research and training identity (`cri-jobs`) can read only `examined/` objects in the research-data bucket, enforced by an IAM condition. Locked data are not published there at all.
- **Validation.** Loaders refuse a dataset whose tier, boundary, checksums or observation dates do not satisfy the boundary. A caller-supplied date is a third check, not the only one.
- **Release after evaluation.** Once RC1's formal locked-period evaluation is complete and recorded, the locked period becomes examined data under `docs/governance/LOCKED_PERIOD_PROTOCOL.md`. A new examined-through date is then set by a signed governance decision, a new boundary constant is released, and the datasets are republished. Research is therefore not permanently confined to November 2026 data.
