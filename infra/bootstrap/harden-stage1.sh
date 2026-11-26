#!/usr/bin/env bash
# Hardening STAGE 1 (idempotent; Cloud Shell, organisation admin).
#
# Creates one Terraform identity and one versioned state bucket per
# environment, migrates state, and VERIFIES it. It does NOT touch the old
# shared identity: that is stage 2, which refuses to run until the
# environment-specific identities have authenticated successfully from GitHub
# (workflow "GCP Identity Verification") and isolation has been confirmed.
#
#   terraform-dev     owner on the dev project only,        state ...-tfstate-dev
#   terraform-staging owner on the staging project only,    state ...-tfstate-staging
#   terraform-prod    owner on the production project only, state ...-tfstate-production
#
# Production images get their own Artifact Registry repository (cri-prod),
# writable only by the production deployer and readable only by production's
# Cloud Run agent, so dev and staging can never publish what production runs.
set -euo pipefail
: "${BILLING_ACCOUNT:?set BILLING_ACCOUNT}"
SUFFIX="${SUFFIX:-cda9}"; REGION="${REGION:-europe-west1}"
OPS="tiva-cri-ops-${SUFFIX}"
declare -A PROJ=([dev]="tiva-cri-dev-${SUFFIX}" [staging]="tiva-cri-stg-${SUFFIX}" [production]="tiva-cri-prd-${SUFFIX}")
declare -A SA_ID=([dev]=terraform-dev [staging]=terraform-staging [production]=terraform-prod)
declare -A BUDGET=([dev]=40 [staging]=120 [production]=600)
OPS_NUMBER="$(gcloud projects describe "$OPS" --format='value(projectNumber)')"
POOL="projects/${OPS_NUMBER}/locations/global/workloadIdentityPools/github"
OLD_SA="terraform@${OPS}.iam.gserviceaccount.com"
OLD_BUCKET="gs://${OPS}-tfstate"

for env in dev staging production; do
  p="${PROJ[$env]}"; sa="${SA_ID[$env]}@${OPS}.iam.gserviceaccount.com"; bucket="gs://${OPS}-tfstate-${env}"
  gcloud iam service-accounts describe "$sa" --project "$OPS" >/dev/null 2>&1 || \
    gcloud iam service-accounts create "${SA_ID[$env]}" --project "$OPS" --display-name "CRI Terraform (${env} only)"
  for i in 1 2 3 4 5; do gcloud iam service-accounts describe "$sa" --project "$OPS" >/dev/null 2>&1 && break; sleep 5; done
  gcloud projects add-iam-policy-binding "$p" --member "serviceAccount:${sa}" --role roles/owner --condition=None >/dev/null
  if ! gcloud storage buckets describe "$bucket" >/dev/null 2>&1; then
    gcloud storage buckets create "$bucket" --project "$OPS" --location "$REGION" \
      --uniform-bucket-level-access --public-access-prevention
  fi
  gcloud storage buckets update "$bucket" --versioning >/dev/null
  gcloud storage buckets add-iam-policy-binding "$bucket" --member "serviceAccount:${sa}" --role roles/storage.objectAdmin >/dev/null
  # Migrate existing state once (old shared bucket, same prefix).
  if gcloud storage ls "${OLD_BUCKET}/cri/${env}/default.tfstate" >/dev/null 2>&1 && \
     ! gcloud storage ls "${bucket}/cri/${env}/default.tfstate" >/dev/null 2>&1; then
    gcloud storage cp "${OLD_BUCKET}/cri/${env}/default.tfstate" "${bucket}/cri/${env}/default.tfstate"
    echo "migrated state for ${env}"
  fi
  if [ "$env" = "production" ]; then
    member="principalSet://iam.googleapis.com/${POOL}/attribute.env_ref/terraform-production@refs/heads/main"
  else
    member="principalSet://iam.googleapis.com/${POOL}/attribute.environment/terraform-${env}"
  fi
  gcloud iam service-accounts add-iam-policy-binding "$sa" --project "$OPS" --role roles/iam.workloadIdentityUser \
    --member "$member" >/dev/null
  # Artifact Registry: the environment's Cloud Run agent pulls, its deployer pushes.
  pnum="$(gcloud projects describe "$p" --format='value(projectNumber)')"
  gcloud artifacts repositories add-iam-policy-binding cri --project "$OPS" --location "$REGION" \
    --member "serviceAccount:service-${pnum}@serverless-robot-prod.iam.gserviceaccount.com" \
    --role roles/artifactregistry.reader >/dev/null 2>&1 || echo "note: ${env} Cloud Run agent not created yet"
  if gcloud iam service-accounts describe "cri-deployer@${p}.iam.gserviceaccount.com" --project "$p" >/dev/null 2>&1; then
    gcloud artifacts repositories add-iam-policy-binding cri --project "$OPS" --location "$REGION" \
      --member "serviceAccount:cri-deployer@${p}.iam.gserviceaccount.com" --role roles/artifactregistry.writer >/dev/null
  fi
  # Budgets (billing-account level), created once per project.
  if [ -z "$(gcloud billing budgets list --billing-project "$OPS" --billing-account "$BILLING_ACCOUNT" \
        --filter="displayName='CRI ${env} monthly'" --format='value(name)' 2>/dev/null)" ]; then
    gcloud billing budgets create --billing-project "$OPS" --billing-account "$BILLING_ACCOUNT" --display-name "CRI ${env} monthly" \
      --budget-amount "${BUDGET[$env]}USD" --filter-projects "projects/${pnum}" --credit-types-treatment exclude-all-credits \
      --threshold-rule percent=0.5 --threshold-rule percent=0.9 --threshold-rule percent=1.0 \
      --threshold-rule percent=1.0,basis=forecasted-spend >/dev/null && echo "budget created for ${env}"
  fi
done


# Production image repository (separate from the shared dev/staging repository).
gcloud artifacts repositories describe cri-prod --project "$OPS" --location "$REGION" >/dev/null 2>&1 || \
  gcloud artifacts repositories create cri-prod --project "$OPS" --location "$REGION" --repository-format docker \
    --description "CRI production images (production deployer only)"
ppnum="$(gcloud projects describe "${PROJ[production]}" --format='value(projectNumber)')"
gcloud artifacts repositories add-iam-policy-binding cri-prod --project "$OPS" --location "$REGION" \
  --member "serviceAccount:service-${ppnum}@serverless-robot-prod.iam.gserviceaccount.com" \
  --role roles/artifactregistry.reader >/dev/null 2>&1 || echo "note: production Cloud Run agent not created yet"
if gcloud iam service-accounts describe "cri-deployer@${PROJ[production]}.iam.gserviceaccount.com" \
     --project "${PROJ[production]}" >/dev/null 2>&1; then
  gcloud artifacts repositories add-iam-policy-binding cri-prod --project "$OPS" --location "$REGION" \
    --member "serviceAccount:cri-deployer@${PROJ[production]}.iam.gserviceaccount.com" \
    --role roles/artifactregistry.writer >/dev/null
fi
# Production must not read the shared repository.
gcloud artifacts repositories remove-iam-policy-binding cri --project "$OPS" --location "$REGION" \
  --member "serviceAccount:service-${ppnum}@serverless-robot-prod.iam.gserviceaccount.com" \
  --role roles/artifactregistry.reader >/dev/null 2>&1 || true

echo "--- stage 1 verification"
fail=0
for env in dev staging production; do
  sa="${SA_ID[$env]}@${OPS}.iam.gserviceaccount.com"; bucket="gs://${OPS}-tfstate-${env}"
  for e2 in dev staging production; do
    p="${PROJ[$e2]}"
    roles=$(gcloud projects get-iam-policy "$p" --flatten='bindings[].members' \
      --filter="bindings.members:serviceAccount:${sa}" --format='value(bindings.role)' | tr '\n' ' ')
    echo "${SA_ID[$env]} on ${p}: ${roles:-none}"
    if [ "$e2" != "$env" ] && [ -n "$roles" ]; then echo "FAIL: ${SA_ID[$env]} has roles outside ${env}"; fail=1; fi
  done
  old="${OLD_BUCKET}/cri/${env}/default.tfstate"; new="${bucket}/cri/${env}/default.tfstate"
  if gcloud storage ls "$old" >/dev/null 2>&1; then
    if ! gcloud storage ls "$new" >/dev/null 2>&1; then echo "FAIL: ${env} state not migrated"; fail=1; continue; fi
    gcloud storage cat "$old" > "/tmp/old-${env}.json"; gcloud storage cat "$new" > "/tmp/new-${env}.json"
    python3 - "$env" <<'PY' || fail=1
import json, sys
env = sys.argv[1]
o, n = (json.load(open(f"/tmp/{k}-{env}.json")) for k in ("old", "new"))
ok = o["lineage"] == n["lineage"] and n["serial"] >= o["serial"] and len(n["resources"]) >= len(o["resources"]) * 0.9
print(f"{env} state: lineage {'same' if o['lineage'] == n['lineage'] else 'DIFFERENT'}, serial {o['serial']} -> {n['serial']}, "
      f"resources {len(o['resources'])} -> {len(n['resources'])}: {'OK' if ok else 'FAIL'}")
sys.exit(0 if ok else 1)
PY
  else
    echo "${env} state: none in the old bucket (new environment)"
  fi
done
echo "old shared SA still enabled (stage 2 retires it): $(gcloud iam service-accounts describe "$OLD_SA" --project "$OPS" --format='value(disabled)' | sed 's/^$/False/')"
if [ $fail = 1 ]; then echo "STAGE 1 FAILED: fix the items above before stage 2"; exit 1; fi
echo "STAGE 1 COMPLETE. Next: push infra/requests/identity-verify.json (engineering does this) and run stage 2 with the successful run id."
