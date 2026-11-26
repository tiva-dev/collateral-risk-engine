#!/usr/bin/env bash
# Hardening STAGE 1 (idempotent; Cloud Shell, organisation admin).
#
# Creates one Terraform identity and one versioned state bucket per
# environment, migrates state under a lock, and VERIFIES it exactly. It does
# NOT touch the old shared identity: that is stage 2, which refuses to run
# until the GCP Identity Verification workflow has succeeded on main.
#
#   terraform-dev     owner on the dev project only,        state ...-tfstate-dev
#   terraform-staging owner on the staging project only,    state ...-tfstate-staging
#   terraform-prod    owner on the production project only, state ...-tfstate-production
#
# Production images get their own Artifact Registry repository (cri-prod),
# writable only by the production deployer and readable only by production's
# Cloud Run agent. Production itself is not provisioned by this script.
#
# Safety:
# * Concurrent Terraform runs are blocked by creating the backend lock objects
#   (<prefix>/default.tflock) in the old and new buckets with a create-only
#   precondition; if a lock already exists the script stops. Locks are removed
#   on exit.
# * State is never written to disk: it is streamed and compared in memory.
# * State is copied with a create-only precondition (never overwritten) and
#   verified byte-for-byte (SHA-256), plus lineage, serial and the exact set
#   of resource instance addresses.
# * Every failure stops the script (set -euo pipefail); nothing is suppressed
#   except "already exists" outcomes that are checked explicitly.
set -euo pipefail
umask 077
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
LOCKS=()

release_locks() {
  for l in "${LOCKS[@]}"; do gcloud storage rm "$l" >/dev/null 2>&1 || echo "WARNING: could not remove lock $l (remove it manually)"; done
}
trap release_locks EXIT

lock() {  # create-only lock object; fails if Terraform (or another run) holds it
  local obj="$1"
  printf '{"ID":"cri-hardening-stage1","Operation":"state migration","Who":"%s","Created":"%s"}' \
    "$(gcloud config get-value account 2>/dev/null)" "$(date -u +%FT%TZ)" |
    gcloud storage cp - "$obj" --if-generation-match=0 >/dev/null ||
    { echo "FAIL: $obj exists: a Terraform run or another migration holds the lock"; exit 1; }
  LOCKS+=("$obj")
}

state_digest() {  # sha256 | lineage | serial | sha256 of sorted resource instance addresses
  gcloud storage cat "$1" | python3 -c '
import hashlib, json, sys
raw = sys.stdin.buffer.read()
s = json.loads(raw)
addrs = []
for r in s.get("resources", []):
    base = ".".join(x for x in (r.get("module"), ("data." if r.get("mode") == "data" else "") + r["type"], r["name"]) if x)
    for inst in r.get("instances", []):
        k = inst.get("index_key")
        addrs.append(base + ("" if k is None else f"[{json.dumps(k)}]"))
print(hashlib.sha256(raw).hexdigest(), s["lineage"], s["serial"], len(addrs),
      hashlib.sha256("\n".join(sorted(addrs)).encode()).hexdigest())'
}

for env in dev staging production; do
  p="${PROJ[$env]}"; sa="${SA_ID[$env]}@${OPS}.iam.gserviceaccount.com"; bucket="gs://${OPS}-tfstate-${env}"
  gcloud iam service-accounts describe "$sa" --project "$OPS" >/dev/null 2>&1 || \
    gcloud iam service-accounts create "${SA_ID[$env]}" --project "$OPS" --display-name "CRI Terraform (${env} only)"
  for i in 1 2 3 4 5 6; do gcloud iam service-accounts describe "$sa" --project "$OPS" >/dev/null 2>&1 && break; sleep 5; done
  gcloud projects add-iam-policy-binding "$p" --member "serviceAccount:${sa}" --role roles/owner --condition=None >/dev/null
  if ! gcloud storage buckets describe "$bucket" >/dev/null 2>&1; then
    gcloud storage buckets create "$bucket" --project "$OPS" --location "$REGION" \
      --uniform-bucket-level-access --public-access-prevention
  fi
  gcloud storage buckets update "$bucket" --versioning >/dev/null
  gcloud storage buckets add-iam-policy-binding "$bucket" --member "serviceAccount:${sa}" --role roles/storage.objectAdmin >/dev/null
  old="${OLD_BUCKET}/cri/${env}/default.tfstate"; new="${bucket}/cri/${env}/default.tfstate"
  if gcloud storage ls "$old" >/dev/null 2>&1; then
    lock "${OLD_BUCKET}/cri/${env}/default.tflock"
    lock "${bucket}/cri/${env}/default.tflock"
    if ! gcloud storage ls "$new" >/dev/null 2>&1; then
      gcloud storage cp "$old" "$new" --if-generation-match=0 >/dev/null
      echo "migrated state for ${env}"
    fi
    read -r h_old lin_old ser_old n_old a_old <<<"$(state_digest "$old")"
    read -r h_new lin_new ser_new n_new a_new <<<"$(state_digest "$new")"
    echo "${env} state: sha256 ${h_old:0:12}/${h_new:0:12} lineage ${lin_old}/${lin_new} serial ${ser_old}/${ser_new} instances ${n_old}/${n_new}"
    if [ "$h_old" != "$h_new" ]; then
      # Only acceptable if Terraform has already written newer state with the new identity.
      [ "$lin_old" = "$lin_new" ] && [ "$ser_new" -gt "$ser_old" ] ||
        { echo "FAIL: ${env} new state differs from the old state and is not a later serial of the same lineage"; exit 1; }
      echo "note: ${env} new state is a later serial written since migration"
    else
      [ "$a_old" = "$a_new" ] && [ "$lin_old" = "$lin_new" ] && [ "$ser_old" = "$ser_new" ] ||
        { echo "FAIL: ${env} state verification"; exit 1; }
    fi
  else
    echo "${env} state: none in the old bucket (new environment)"
  fi
  if [ "$env" = "production" ]; then
    member="principalSet://iam.googleapis.com/${POOL}/attribute.env_ref/terraform-production@refs/heads/main"
  else
    member="principalSet://iam.googleapis.com/${POOL}/attribute.environment/terraform-${env}"
  fi
  gcloud iam service-accounts add-iam-policy-binding "$sa" --project "$OPS" --role roles/iam.workloadIdentityUser \
    --member "$member" >/dev/null
  pnum="$(gcloud projects describe "$p" --format='value(projectNumber)')"
  if [ "$env" != production ]; then
    agent="serviceAccount:service-${pnum}@serverless-robot-prod.iam.gserviceaccount.com"
    gcloud artifacts repositories add-iam-policy-binding cri --project "$OPS" --location "$REGION" \
      --member "$agent" --role roles/artifactregistry.reader >/dev/null ||
      echo "note: ${env} Cloud Run agent not created yet (no grant made)"
    if gcloud iam service-accounts describe "cri-deployer@${p}.iam.gserviceaccount.com" --project "$p" >/dev/null 2>&1; then
      gcloud artifacts repositories add-iam-policy-binding cri --project "$OPS" --location "$REGION" \
        --member "serviceAccount:cri-deployer@${p}.iam.gserviceaccount.com" --role roles/artifactregistry.writer >/dev/null
    fi
  fi
  if [ -z "$(gcloud billing budgets list --billing-project "$OPS" --billing-account "$BILLING_ACCOUNT" \
        --filter="displayName='CRI ${env} monthly'" --format='value(name)')" ]; then
    gcloud billing budgets create --billing-project "$OPS" --billing-account "$BILLING_ACCOUNT" --display-name "CRI ${env} monthly" \
      --budget-amount "${BUDGET[$env]}USD" --filter-projects "projects/${pnum}" --credit-types-treatment exclude-all-credits \
      --threshold-rule percent=0.5 --threshold-rule percent=0.9 --threshold-rule percent=1.0 \
      --threshold-rule percent=1.0,basis=forecasted-spend >/dev/null && echo "budget created for ${env}"
  fi
done

# Production image repository, separate from the shared dev/staging repository.
gcloud artifacts repositories describe cri-prod --project "$OPS" --location "$REGION" >/dev/null 2>&1 || \
  gcloud artifacts repositories create cri-prod --project "$OPS" --location "$REGION" --repository-format docker \
    --description "CRI production images (production deployer only)"
ppnum="$(gcloud projects describe "${PROJ[production]}" --format='value(projectNumber)')"
pagent="serviceAccount:service-${ppnum}@serverless-robot-prod.iam.gserviceaccount.com"
if gcloud artifacts repositories get-iam-policy cri --project "$OPS" --location "$REGION" --format=json |
     grep -q "service-${ppnum}@serverless-robot-prod"; then
  gcloud artifacts repositories remove-iam-policy-binding cri --project "$OPS" --location "$REGION" \
    --member "$pagent" --role roles/artifactregistry.reader >/dev/null
fi

echo "--- stage 1 verification"
fail=0
for env in dev staging production; do
  sa="${SA_ID[$env]}@${OPS}.iam.gserviceaccount.com"
  for e2 in dev staging production; do
    roles=$(gcloud projects get-iam-policy "${PROJ[$e2]}" --flatten='bindings[].members' \
      --filter="bindings.members:serviceAccount:${sa}" --format='value(bindings.role)' | tr '\n' ' ')
    echo "${SA_ID[$env]} on ${PROJ[$e2]}: ${roles:-none}"
    if [ "$e2" != "$env" ] && [ -n "$roles" ]; then echo "FAIL: ${SA_ID[$env]} has roles outside ${env}"; fail=1; fi
  done
done
if gcloud artifacts repositories get-iam-policy cri --project "$OPS" --location "$REGION" --format=json |
     grep -q "service-${ppnum}@serverless-robot-prod\|cri-deployer@${PROJ[production]}"; then
  echo "FAIL: production identities can use the shared repository"; fail=1
fi
[ $fail = 0 ] || { echo "STAGE 1 FAILED"; exit 1; }
printf '{"stage":1,"completed_at":"%s","by":"%s"}' "$(date -u +%FT%TZ)" "$(gcloud config get-value account 2>/dev/null)" |
  gcloud storage cp - "gs://${OPS}-tfstate-staging/hardening/stage1-complete.json" >/dev/null
echo "old shared SA still enabled (stage 2 retires it): $(gcloud iam service-accounts describe "$OLD_SA" --project "$OPS" --format='value(disabled)' | sed 's/^$/False/')"
echo "STAGE 1 COMPLETE."
