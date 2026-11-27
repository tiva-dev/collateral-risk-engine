#!/usr/bin/env bash
# READ-ONLY pre-checks before hardening stage 1. Changes nothing.
# Fails closed: a lookup that errors is reported as FAIL, never as "absent" or OK.
# One line per check and a final verdict; error detail in $HOME/cri-precheck.log.
set -uo pipefail
SUFFIX="${SUFFIX:-cda9}"; OPS="tiva-cri-ops-${SUFFIX}"
EXPECT_ACCOUNT="${EXPECT_ACCOUNT:-gilchrist.e@tivafinance.com}"
EXPECT_ORG_ID="${EXPECT_ORG_ID:?set EXPECT_ORG_ID to the organisation that should own the CRI projects}"
EXPECT_BILLING="${EXPECT_BILLING:-01BDBE-D3BDA3-260FCB}"
LOG="$HOME/cri-precheck.log"; : > "$LOG"
fail=0
ok()   { echo "OK    $*"; }
bad()  { echo "FAIL  $*"; fail=1; }
note() { echo "NOTE  $*"; }
# run CMD...: stdout to caller; on error log stderr and return 2 (distinct from "empty").
run() { local out; if out="$("$@" 2>>"$LOG")"; then printf '%s' "$out"; return 0; else return 2; fi; }
# exists CMD...: 0 = exists, 1 = definitely absent (404/NOT_FOUND), 2 = could not tell.
exists() {
  local err; err="$(mktemp)"
  if "$@" >/dev/null 2>"$err"; then rm -f "$err"; return 0; fi
  if grep -Eqi 'NOT_FOUND|404|does not exist|matched no objects|One or more URLs matched no objects' "$err"; then
    rm -f "$err"; return 1; fi
  cat "$err" >> "$LOG"; rm -f "$err"; return 2
}

acct="$(gcloud config get-value account 2>>"$LOG")"
[ "$acct" = "$EXPECT_ACCOUNT" ] && ok "account $acct" || bad "account is '${acct}', expected $EXPECT_ACCOUNT"

for p in "tiva-cri-ops-${SUFFIX}" "tiva-cri-dev-${SUFFIX}" "tiva-cri-stg-${SUFFIX}" "tiva-cri-prd-${SUFFIX}"; do
  if info="$(run gcloud projects describe "$p" --format='value(lifecycleState,parent.type,parent.id)')"; then
    read -r st ptype pid <<<"$info"
    bill="$(run gcloud billing projects describe "$p" --format='value(billingAccountName,billingEnabled)' | tr '\t' ' ')" \
      || bill="UNREADABLE"
    if [ "$st" = ACTIVE ] && [ "$ptype" = organization ] && [ "$pid" = "$EXPECT_ORG_ID" ]; then
      ok "project $p ACTIVE in organisation $pid; billing: ${bill:-none}"
    else bad "project $p state=$st parent=$ptype/$pid (expected organisation $EXPECT_ORG_ID)"; fi
  else bad "project $p not readable"; fi
done
if binfo="$(run gcloud billing accounts describe "$EXPECT_BILLING" --format='value(displayName,open)')"; then
  ok "billing account $EXPECT_BILLING: $(echo "$binfo" | tr '\t' ' ')"
else bad "billing account $EXPECT_BILLING not readable"; fi

# No Terraform run in progress: no backend lock objects in the old or new buckets.
for b in "gs://${OPS}-tfstate" "gs://${OPS}-tfstate-dev" "gs://${OPS}-tfstate-staging" "gs://${OPS}-tfstate-production"; do
  exists gcloud storage buckets describe "$b"; rc=$?
  if [ $rc = 1 ]; then note "bucket $b does not exist yet"; continue; fi
  if [ $rc = 2 ]; then bad "cannot read bucket $b"; continue; fi
  if objs="$(run gcloud storage ls -r "$b/")"; then
    locks="$(echo "$objs" | grep 'default.tflock' || true)"
    [ -z "$locks" ] && ok "no Terraform lock in $b" || bad "lock present in $b: $locks"
  else bad "cannot list $b"; fi
done

for env in dev staging production; do
  o="gs://${OPS}-tfstate/cri/${env}/default.tfstate"
  exists gcloud storage objects describe "$o"; rc=$?
  case $rc in
    0) hold="$(run gcloud storage objects describe "$o" --format='value(temporary_hold)')"
       ok "old state present for ${env} (temporary hold: ${hold:-none})";;
    1) note "no old state object for ${env}";;
    *) bad "cannot read old state for ${env}";;
  esac
done

old_sa="terraform@${OPS}.iam.gserviceaccount.com"
if d="$(run gcloud iam service-accounts describe "$old_sa" --project "$OPS" --format='value(disabled)')"; then
  ok "shared identity exists (disabled=${d:-False})"
else bad "shared identity not readable"; fi
for sa in terraform-dev terraform-staging terraform-prod; do
  exists gcloud iam service-accounts describe "${sa}@${OPS}.iam.gserviceaccount.com" --project "$OPS"
  case $? in 0) note "${sa} already exists";; 1) note "${sa} not yet created";; *) bad "cannot read ${sa}";; esac
done

[ $fail = 0 ] && echo "PRECHECK PASSED" || echo "PRECHECK FAILED (see $LOG)"
