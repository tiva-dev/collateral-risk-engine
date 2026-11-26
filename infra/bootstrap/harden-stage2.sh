#!/usr/bin/env bash
# Hardening STAGE 2: retire the old shared Terraform identity.
#
# Refuses to run unless VERIFIED_RUN_ID names a successful run of the
# "GCP Identity Verification" workflow on this repository, in which every
# environment-specific identity authenticated through Workload Identity
# Federation, read its own state and failed to reach the other environments.
# Then it re-checks stage 1, removes every grant of the old identity and
# disables it (it is not deleted, so it can be re-enabled if needed).
set -euo pipefail
: "${BILLING_ACCOUNT:?set BILLING_ACCOUNT}"
: "${VERIFIED_RUN_ID:?set VERIFIED_RUN_ID to the successful GCP Identity Verification run id}"
REPO_SLUG="${REPO_SLUG:-tiva-dev/collateral-risk-engine}"
SUFFIX="${SUFFIX:-cda9}"
OPS="tiva-cri-ops-${SUFFIX}"
declare -A PROJ=([dev]="tiva-cri-dev-${SUFFIX}" [staging]="tiva-cri-stg-${SUFFIX}" [production]="tiva-cri-prd-${SUFFIX}")
declare -A SA_ID=([dev]=terraform-dev [staging]=terraform-staging [production]=terraform-prod)
OLD_SA="terraform@${OPS}.iam.gserviceaccount.com"
OLD_BUCKET="gs://${OPS}-tfstate"

run=$(curl -fsSL "https://api.github.com/repos/${REPO_SLUG}/actions/runs/${VERIFIED_RUN_ID}")
python3 - "$run" <<'PY'
import json, sys
r = json.loads(sys.argv[1])
ok = r.get("name") == "GCP Identity Verification" and r.get("conclusion") == "success"
print(f"verification run {r.get('id')}: {r.get('name')} / {r.get('conclusion')} at {r.get('head_sha', '')[:12]}")
sys.exit(0 if ok else 1)
PY
jobs=$(curl -fsSL "https://api.github.com/repos/${REPO_SLUG}/actions/runs/${VERIFIED_RUN_ID}/jobs")
python3 - "$jobs" <<'PY'
import json, sys
j = json.loads(sys.argv[1])["jobs"]
need = {"verify (dev)", "verify (staging)", "verify (production)"}
done = {x["name"] for x in j if x["conclusion"] == "success"}
missing = need - done
print("jobs:", ", ".join(f"{x['name']}={x['conclusion']}" for x in j))
sys.exit(1 if missing else 0)
PY

for env in dev staging production; do
  sa="${SA_ID[$env]}@${OPS}.iam.gserviceaccount.com"
  gcloud iam service-accounts describe "$sa" --project "$OPS" >/dev/null || { echo "FAIL: $sa missing (run stage 1)"; exit 1; }
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

for b in $(gcloud iam service-accounts get-iam-policy "$OLD_SA" --project "$OPS" --format=json | \
           python3 -c 'import json,sys; [print(m) for b in json.load(sys.stdin).get("bindings",[]) for m in b["members"]]'); do
  gcloud iam service-accounts remove-iam-policy-binding "$OLD_SA" --project "$OPS" --role roles/iam.workloadIdentityUser --member "$b" >/dev/null 2>&1 || true
done
gcloud iam service-accounts disable "$OLD_SA" --project "$OPS" >/dev/null 2>&1 || true

echo "--- stage 2 verification"
for p in "${PROJ[@]}" "$OPS"; do
  roles=$(gcloud projects get-iam-policy "$p" --flatten='bindings[].members' --filter="bindings.members:serviceAccount:${OLD_SA}" --format='value(bindings.role)' | tr '\n' ' ')
  echo "old SA on ${p}: ${roles:-none}"
done
echo "old shared SA disabled: $(gcloud iam service-accounts describe "$OLD_SA" --project "$OPS" --format='value(disabled)')"
echo "STAGE 2 COMPLETE"
