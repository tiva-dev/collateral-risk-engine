# Database credential rotation

## Current mechanism (dev and staging)

Set `db_credential_rotation` in the environment's `main.tf` to a new label (for example the date), then apply through the Terraform workflow. Terraform:
- regenerates the `cri_owner` and `cri_app` passwords;
- updates both Cloud SQL users;
- replaces the `cri-database-url-owner` and `cri-database-url-app` secret versions.

Then **immediately** run *GCP Deploy* for the same environment. New revisions and job executions read the new secret versions.

**Known limitation.** Between the apply and the redeploy, the serving revisions hold the old password. New database connections fail until traffic moves; on staging, 27 Nov 2026, this window was about 6 minutes. This is acceptable for dev and staging only.

**Never remove or blank the label** once it is set. Blanking it removes the keepers, which replaces the passwords again.

## Required before production (tracked)

Rotation must be zero-downtime, using two alternating app credentials:
1. Create the inactive user (`cri_app_b`) with the new password. It inherits a shared group role that holds the grants, so migrations grant to the group, not to a login user.
2. Point the secret at the inactive user and deploy. The new revisions connect as `cri_app_b`.
3. After traffic has moved and the old revisions have drained, rotate or disable `cri_app`.

The owner credential, used only by migration jobs, follows the same pattern or is rotated between job runs.

## Staging record

| Rotation | Date | Apply run | Redeploy run | Verified |
|---|---|---|---|---|
| 1 | 2026-11-27 | 46067312421 | 46067709767 | Migration connects as owner; API ready on schema 8 as the app user; health check OK; no authentication errors in worker logs |
