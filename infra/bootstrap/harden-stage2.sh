#!/usr/bin/env bash
# Hardening STAGE 2: retire the old shared Terraform identity.
#
# Runs only when ALL of the following hold, checked here, not assumed:
# 1. VERIFIED_RUN_ID is a run of .github/workflows/gcp-identity-verify.yml
#    ("GCP Identity Verification") on this repository, on branch main, at
#    commit TRUSTED_SHA (supplied by engineering and the founder), concluded
#    "success", started after stage 1 completed, with the jobs verify (dev),
#    verify (staging) and verify (production) all successful.
# 2. The current configuration still matches stage 1: each environment
#    identity exists, is enabled, has roles only on its own project and is
#    impersonable only through its intended Workload Identity principal.
# Then it removes every grant of the old identity, failing on any removal
# error, disables it (not deleted) and verifies that no grant remains.
set -euo pipefail
: "${BILLING_ACCOUNT:?set BILLING_ACCOUNT}"
: "${VERIFIED_RUN_ID:?set VERIFIED_RUN_ID}"
: "${TRUSTED_SHA:?set TRUSTED_SHA (full 40-character commit on main)}"
REPO_SLUG="${REPO_SLUG:-tiva-dev/collateral-risk-engine}"
SUFFIX="${SUFFIX:-cda9}"; REGION="${REGION:-europe-west1}"
OPS="tiva-cri-ops-${SUFFIX}"
declare -A PROJ=([dev]="tiva-cri-dev-${SUFFIX}" [staging]="tiva-cri-stg-${SUFFIX}" [production]="tiva-cri-prd-${SUFFIX}")
declare -A SA_ID=([dev]=terraform-dev [staging]=terraform-staging [production]=terraform-prod)
OPS_NUMBER="$(gcloud projects describe "$OPS" --format='value(projectNumber)')"
POOL="projects/${OPS_NUMBER}/locations/global/workloadIdentityPools/github"
OLD_SA="terraform@${OPS}.iam.gserviceaccount.com"
OLD_BUCKET="gs://${OPS}-tfstate"
API="https://api.github.com/repos/${REPO_SLUG}"

stage1=$(gcloud storage cat "gs://${OPS}-tfstate-staging/hardening/stage1-complete.json")
run=$(curl -fsSL "${API}/actions/runs/${VERIFIED_RUN_ID}")
jobs=$(curl -fsSL "${API}/actions/runs/${VERIFIED_RUN_ID}/jobs")
main_has=$(curl -fsSL "${API}/compare/${TRUSTED_SHA}...main" | python3 -c 'import json,sys; print(json.load(sys.stdin)["status"])')
python3 - "$run" "$jobs" "$stage1" "$TRUSTED_SHA" "$main_has" <<'PY'
import json, sys
run, jobs, stage1 = (json.loads(x) for x in sys.argv[1:4])
sha, rel = sys.argv[4], sys.argv[5]
checks = {
    "workflow path": run.get("path") == ".github/workflows/gcp-identity-verify.yml",
    "workflow name": run.get("name") == "GCP Identity Verification",
    "branch main": run.get("head_branch") == "main",
    "trusted revision": run.get("head_sha") == sha and len(sha) == 40,
    "revision on main": rel in ("identical", "ahead"),
    "concluded success": run.get("conclusion") == "success",
    "after stage 1": run.get("run_started_at", "") > stage1["completed_at"],
}
done = {j["name"]: j["conclusion"] for j in jobs["jobs"]}
for name in ("verify (dev)", "verify (staging)", "verify (production)"):
    checks[name] = done.get(name) == "success"
for k, v in checks.items():
    print(f"{'ok  ' if v else 'FAIL'} {k}")
sys.exit(0 if all(checks.values()) else 1)
PY

echo "--- current configuration"
for env in dev staging production; do
  sa="${SA_ID[$env]}@${OPS}.iam.gserviceaccount.com"
  [ "$(gcloud iam service-accounts describe "$sa" --project "$OPS" --format='value(disabled)')" != "True" ] ||
    { echo "FAIL: $sa disabled"; exit 1; }
  for e2 in dev staging production; do
    roles=$(gcloud projects get-iam-policy "${PROJ[$e2]}" --flatten='bindings[].members' \
      --filter="bindings.members:serviceAccount:${sa}" --format='value(bindings.role)')
    if [ "$e2" = "$env" ]; then echo "$roles" | grep -qx roles/owner || { echo "FAIL: $sa lacks owner on ${PROJ[$e2]}"; exit 1; }
    elif [ -n "$roles" ]; then echo "FAIL: $sa has roles on ${PROJ[$e2]}"; exit 1; fi
  done
  if [ "$env" = production ]; then
    want="principalSet://iam.googleapis.com/${POOL}/attribute.env_ref/terraform-production@refs/heads/main"
  else
    want="principalSet://iam.googleapis.com/${POOL}/attribute.environment/terraform-${env}"
  fi
  got=$(gcloud iam service-accounts get-iam-policy "$sa" --project "$OPS" --format=json |
        python3 -c 'import json,sys; print("\n".join(sorted(m for b in json.load(sys.stdin).get("bindings",[]) if b["role"]=="roles/iam.workloadIdentityUser" for m in b["members"])))')
  [ "$got" = "$want" ] || { echo "FAIL: $sa impersonation principals: $got"; exit 1; }
  echo "${SA_ID[$env]}: ok"
done

echo "--- retiring ${OLD_SA}"
remove_project_roles() {  # $1 project
  for role in $(gcloud projects get-iam-policy "$1" --flatten='bindings[].members' \
                 --filter="bindings.members:serviceAccount:${OLD_SA}" --format='value(bindings.role)'); do
    gcloud projects remove-iam-policy-binding "$1" --member "serviceAccount:${OLD_SA}" --role "$role" --all >/dev/null
    echo "removed $role on $1"
  done
}
for p in "${PROJ[@]}" "$OPS"; do remove_project_roles "$p"; done
for role in $(gcloud storage buckets get-iam-policy "$OLD_BUCKET" --format=json |
              python3 -c "import json,sys; [print(b['role']) for b in json.load(sys.stdin).get('bindings',[]) if 'serviceAccount:${OLD_SA}' in b['members']]"); do
  gcloud storage buckets remove-iam-policy-binding "$OLD_BUCKET" --member "serviceAccount:${OLD_SA}" --role "$role" >/dev/null
  echo "removed $role on $OLD_BUCKET"
done
for role in $(gcloud billing accounts get-iam-policy "$BILLING_ACCOUNT" --format=json |
              python3 -c "import json,sys; [print(b['role']) for b in json.load(sys.stdin).get('bindings',[]) if 'serviceAccount:${OLD_SA}' in b['members']]"); do
  gcloud billing accounts remove-iam-policy-binding "$BILLING_ACCOUNT" --member "serviceAccount:${OLD_SA}" --role "$role" >/dev/null
  echo "removed $role on billing account"
done
for repo in cri cri-prod; do
  for role in $(gcloud artifacts repositories get-iam-policy "$repo" --project "$OPS" --location "$REGION" --format=json |
                python3 -c "import json,sys; [print(b['role']) for b in json.load(sys.stdin).get('bindings',[]) if 'serviceAccount:${OLD_SA}' in b['members']]"); do
    gcloud artifacts repositories remove-iam-policy-binding "$repo" --project "$OPS" --location "$REGION" \
      --member "serviceAccount:${OLD_SA}" --role "$role" >/dev/null
    echo "removed $role on repository $repo"
  done
done
gcloud iam service-accounts get-iam-policy "$OLD_SA" --project "$OPS" --format=json |
  python3 -c 'import json,sys; [print(b["role"], m) for b in json.load(sys.stdin).get("bindings",[]) for m in b["members"]]' |
  while read -r role member; do
    gcloud iam service-accounts remove-iam-policy-binding "$OLD_SA" --project "$OPS" --role "$role" --member "$member" >/dev/null
    echo "removed impersonation $role for $member"
  done
gcloud iam service-accounts disable "$OLD_SA" --project "$OPS"
for key in $(gcloud iam service-accounts keys list --iam-account "$OLD_SA" --project "$OPS" --managed-by=user --format='value(name)'); do
  gcloud iam service-accounts keys disable "$key" --iam-account "$OLD_SA" --project "$OPS"
done

echo "--- stage 2 verification"
fail=0
for p in "${PROJ[@]}" "$OPS"; do
  r=$(gcloud projects get-iam-policy "$p" --flatten='bindings[].members' --filter="bindings.members:serviceAccount:${OLD_SA}" --format='value(bindings.role)')
  [ -z "$r" ] || { echo "FAIL: old SA still has $r on $p"; fail=1; }
done
gcloud storage buckets get-iam-policy "$OLD_BUCKET" --format=json | grep -q "$OLD_SA" && { echo "FAIL: old SA on state bucket"; fail=1; }
gcloud billing accounts get-iam-policy "$BILLING_ACCOUNT" --format=json | grep -q "$OLD_SA" && { echo "FAIL: old SA on billing"; fail=1; }
[ -z "$(gcloud iam service-accounts get-iam-policy "$OLD_SA" --project "$OPS" --format='value(bindings)')" ] ||
  { echo "FAIL: old SA still impersonable"; fail=1; }
[ "$(gcloud iam service-accounts describe "$OLD_SA" --project "$OPS" --format='value(disabled)')" = "True" ] ||
  { echo "FAIL: old SA not disabled"; fail=1; }
[ $fail = 0 ] || { echo "STAGE 2 VERIFICATION FAILED"; exit 1; }
echo "old shared SA: disabled, no grants, not impersonable"
echo "STAGE 2 COMPLETE"
