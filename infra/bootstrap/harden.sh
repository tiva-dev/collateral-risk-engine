#!/usr/bin/env bash
# Hardening step 1 (idempotent; run in Cloud Shell as an organisation admin).
#
# Replaces the single cross-environment Terraform identity with one identity
# and one state bucket per environment:
#   terraform-dev     owner on the dev project only,     state bucket ...-tfstate-dev
#   terraform-staging owner on the staging project only, state bucket ...-tfstate-staging
#   terraform-prod    owner on the production project only, state bucket ...-tfstate-production
# Each is impersonable only from its GitHub environment; production additionally
# only from refs/heads/main. Shared resources that used to need cross-project
# rights (Artifact Registry pull/push grants, budgets) are managed here instead.
# The old shared identity loses every grant and is disabled (not deleted).
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

# Retire the shared identity: remove every grant, then disable it.
for p in "${PROJ[@]}"; do
  gcloud projects remove-iam-policy-binding "$p" --member "serviceAccount:${OLD_SA}" --role roles/owner \
    --condition=None >/dev/null 2>&1 || true
done
gcloud projects remove-iam-policy-binding "$OPS" --member "serviceAccount:${OLD_SA}" --role roles/artifactregistry.admin --condition=None >/dev/null 2>&1 || true
gcloud projects remove-iam-policy-binding "$OPS" --member "serviceAccount:${OLD_SA}" --role roles/iam.workloadIdentityPoolViewer --condition=None >/dev/null 2>&1 || true
gcloud storage buckets remove-iam-policy-binding "$OLD_BUCKET" --member "serviceAccount:${OLD_SA}" --role roles/storage.objectAdmin >/dev/null 2>&1 || true
gcloud billing accounts remove-iam-policy-binding "$BILLING_ACCOUNT" --member "serviceAccount:${OLD_SA}" --role roles/billing.costsManager >/dev/null 2>&1 || true
gcloud billing accounts remove-iam-policy-binding "$BILLING_ACCOUNT" --member "serviceAccount:${OLD_SA}" --role roles/billing.viewer >/dev/null 2>&1 || true
for b in $(gcloud iam service-accounts get-iam-policy "$OLD_SA" --project "$OPS" --format=json | \
           python3 -c 'import json,sys; [print(m) for b in json.load(sys.stdin).get("bindings",[]) for m in b["members"]]'); do
  gcloud iam service-accounts remove-iam-policy-binding "$OLD_SA" --project "$OPS" --role roles/iam.workloadIdentityUser --member "$b" >/dev/null 2>&1 || true
done
gcloud iam service-accounts disable "$OLD_SA" --project "$OPS" >/dev/null 2>&1 || true

echo "--- verification"
for env in dev staging production; do
  sa="${SA_ID[$env]}@${OPS}.iam.gserviceaccount.com"
  for p in "${PROJ[@]}"; do
    roles=$(gcloud projects get-iam-policy "$p" --flatten='bindings[].members' --filter="bindings.members:serviceAccount:${sa}" --format='value(bindings.role)' | tr '\n' ' ')
    echo "${SA_ID[$env]} on ${p}: ${roles:-none}"
  done
done
echo "old shared SA disabled: $(gcloud iam service-accounts describe "$OLD_SA" --project "$OPS" --format='value(disabled)')"
echo "hardening complete"
