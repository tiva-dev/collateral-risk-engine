#!/usr/bin/env bash
# One-time bootstrap for CRI on Google Cloud. Run in Cloud Shell as a user who
# is Billing Account Administrator on the billing account that holds the
# credits. Idempotent: re-running only creates what is missing.
#
# Creates:
#   - projects: ops (state, images, CI identity), dev, staging, production
#   - billing links for all four
#   - GCS bucket for Terraform state (versioned, uniform access, no public access)
#   - Artifact Registry repository for CRI images (in ops)
#   - service account "terraform" (ops) with owner on dev/staging/production,
#     Artifact Registry admin on ops and budget management on the billing account
#   - Workload Identity Federation pool "github" restricted to the CRI repository;
#     GitHub jobs can impersonate "terraform" only from the GitHub environments
#     terraform-dev, terraform-staging and terraform-production
#
# It does NOT create Cloud SQL, Cloud Run or any other billable runtime resource;
# those are created by Terraform per environment.
set -euo pipefail

: "${BILLING_ACCOUNT:?set BILLING_ACCOUNT to the billing account id (XXXXXX-XXXXXX-XXXXXX)}"
PREFIX="${PREFIX:-tiva-cri}"
SUFFIX="${SUFFIX:-$(echo -n "$BILLING_ACCOUNT" | sha256sum | cut -c1-4)}"
REGION="${REGION:-europe-west1}"
REPO="${GITHUB_REPO:-tiva-dev/collateral-risk-engine}"
ORG="${ORG:-930851692765}"

OPS="${PREFIX}-ops-${SUFFIX}"
DEV="${PREFIX}-dev-${SUFFIX}"
STG="${PREFIX}-stg-${SUFFIX}"
PRD="${PREFIX}-prd-${SUFFIX}"

ensure_project () {
  local id="$1" name="$2" link="$3"
  if ! gcloud projects describe "$id" >/dev/null 2>&1; then
    gcloud projects create "$id" --name="$name" --organization="$ORG" --labels=system=cri,owner=tiva
  fi
  if [ "$link" = "link" ]; then
    gcloud billing projects link "$id" --billing-account="$BILLING_ACCOUNT" >/dev/null
  fi
}

ensure_project "$OPS" "CRI ops" link
ensure_project "$DEV" "CRI dev" link
ensure_project "$STG" "CRI staging" link
# Production is created but NOT linked to billing: nothing billable can run in
# it until Tiva approves production spend and links it (LINK_PRODUCTION=1).
ensure_project "$PRD" "CRI production" "${LINK_PRODUCTION:+link}"

gcloud services enable --project "$OPS" \
  iam.googleapis.com iamcredentials.googleapis.com sts.googleapis.com cloudresourcemanager.googleapis.com \
  serviceusage.googleapis.com storage.googleapis.com artifactregistry.googleapis.com \
  cloudbilling.googleapis.com billingbudgets.googleapis.com
gcloud services enable --project "$OPS" orgpolicy.googleapis.com
for p in "$DEV" "$STG" ${LINK_PRODUCTION:+"$PRD"}; do
  gcloud services enable --project "$p" serviceusage.googleapis.com cloudresourcemanager.googleapis.com \
    iam.googleapis.com cloudbilling.googleapis.com billingbudgets.googleapis.com
done

STATE_BUCKET="${OPS}-tfstate"
if ! gcloud storage buckets describe "gs://${STATE_BUCKET}" >/dev/null 2>&1; then
  gcloud storage buckets create "gs://${STATE_BUCKET}" --project "$OPS" --location "$REGION" \
    --uniform-bucket-level-access --public-access-prevention
fi
gcloud storage buckets update "gs://${STATE_BUCKET}" --versioning

if ! gcloud artifacts repositories describe cri --project "$OPS" --location "$REGION" >/dev/null 2>&1; then
  gcloud artifacts repositories create cri --project "$OPS" --location "$REGION" \
    --repository-format docker --description "CRI service and research images"
fi

TF_SA="terraform@${OPS}.iam.gserviceaccount.com"
gcloud iam service-accounts describe "$TF_SA" --project "$OPS" >/dev/null 2>&1 || \
  gcloud iam service-accounts create terraform --project "$OPS" --display-name "CRI Terraform (GitHub Actions)"
for p in "$DEV" "$STG" "$PRD"; do
  gcloud projects add-iam-policy-binding "$p" --member "serviceAccount:${TF_SA}" --role roles/owner --condition=None >/dev/null
done
gcloud projects add-iam-policy-binding "$OPS" --member "serviceAccount:${TF_SA}" --role roles/artifactregistry.admin --condition=None >/dev/null
gcloud projects add-iam-policy-binding "$OPS" --member "serviceAccount:${TF_SA}" --role roles/iam.workloadIdentityPoolViewer --condition=None >/dev/null
gcloud storage buckets add-iam-policy-binding "gs://${STATE_BUCKET}" --member "serviceAccount:${TF_SA}" --role roles/storage.objectAdmin >/dev/null
gcloud billing accounts add-iam-policy-binding "$BILLING_ACCOUNT" --member "serviceAccount:${TF_SA}" --role roles/billing.costsManager >/dev/null
gcloud billing accounts add-iam-policy-binding "$BILLING_ACCOUNT" --member "serviceAccount:${TF_SA}" --role roles/billing.viewer >/dev/null

OPS_NUMBER="$(gcloud projects describe "$OPS" --format='value(projectNumber)')"
if ! gcloud iam workload-identity-pools describe github --project "$OPS" --location global >/dev/null 2>&1; then
  gcloud iam workload-identity-pools create github --project "$OPS" --location global --display-name "GitHub Actions"
fi
if ! gcloud iam workload-identity-pools providers describe cri-repo --project "$OPS" --location global \
     --workload-identity-pool github >/dev/null 2>&1; then
  gcloud iam workload-identity-pools providers create-oidc cri-repo --project "$OPS" --location global \
    --workload-identity-pool github --display-name "tiva-dev/collateral-risk-engine" \
    --issuer-uri "https://token.actions.githubusercontent.com" \
    --attribute-mapping "google.subject=assertion.sub,attribute.repository=assertion.repository,attribute.ref=assertion.ref,attribute.environment=assertion.environment,attribute.actor=assertion.actor" \
    --attribute-condition "assertion.repository == '${REPO}'"
fi
POOL="projects/${OPS_NUMBER}/locations/global/workloadIdentityPools/github"
for env in terraform-dev terraform-staging terraform-production; do
  gcloud iam service-accounts add-iam-policy-binding "$TF_SA" --project "$OPS" \
    --role roles/iam.workloadIdentityUser \
    --member "principalSet://iam.googleapis.com/${POOL}/attribute.environment/${env}" >/dev/null
done

echo "--- effective org policies that affect CRI (read-only)"
for c in iam.allowedPolicyMemberDomains sql.restrictPublicIp sql.restrictAuthorizedNetworks \
         iam.workloadIdentityPoolProviders run.allowedIngress gcp.resourceLocations \
         iam.automaticIamGrantsForDefaultServiceAccounts storage.publicAccessPrevention; do
  printf '%s => ' "$c"
  gcloud org-policies describe "$c" --project "$OPS" --effective --billing-project "$OPS" \
    --format='value(spec.rules)' 2>&1 | tr '\n' ' ' | cut -c1-120; echo
done

cat <<OUT
CRI bootstrap complete.
  ops project        ${OPS}
  dev project        ${DEV}
  staging project    ${STG}
  production project ${PRD}
  state bucket       ${STATE_BUCKET}
  artifact registry  ${REGION}-docker.pkg.dev/${OPS}/cri
  terraform SA       ${TF_SA}
  WIF provider       ${POOL}/providers/cri-repo
OUT
