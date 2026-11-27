#!/usr/bin/env bash
# Founder-approved on 27 Nov 2026 and executed in Cloud Shell the same day
# (after hardening stage 2). Read-only, repository-scoped image access for the
# per-environment Terraform identities: updating a Cloud Run service or job
# requires the caller to read its image. The retired shared identity had
# registry admin; stage 1 did not grant the new identities read access.
# Production's repository stays isolated: only terraform-prod reads cri-prod.
set -euo pipefail
O=tiva-cri-ops-cda9; M=iam.gserviceaccount.com
R="--project $O --location europe-west1 --role roles/artifactregistry.reader"
A="gcloud artifacts repositories add-iam-policy-binding"
$A cri $R --member serviceAccount:terraform-dev@$O.$M >/dev/null && echo granted dev
$A cri $R --member serviceAccount:terraform-staging@$O.$M >/dev/null && echo granted staging
$A cri-prod $R --member serviceAccount:terraform-prod@$O.$M >/dev/null && echo granted prod
