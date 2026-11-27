#!/usr/bin/env bash
# READ-ONLY pre-checks before hardening stage 1. Changes nothing.
# Prints one line per check and a final verdict; full detail in $HOME/cri-precheck.log.
set -uo pipefail
SUFFIX="${SUFFIX:-cda9}"; OPS="tiva-cri-ops-${SUFFIX}"
EXPECT_ACCOUNT="${EXPECT_ACCOUNT:-gilchrist.e@tivafinance.com}"
EXPECT_ORG_DOMAIN="${EXPECT_ORG_DOMAIN:-tivafinance.com}"
EXPECT_BILLING="${EXPECT_BILLING:-01BDBE-D3BDA3-260FCB}"
LOG="$HOME/cri-precheck.log"; : > "$LOG"
fail=0
ok()   { echo "OK    $*"; }
bad()  { echo "FAIL  $*"; fail=1; }
note() { echo "NOTE  $*"; }

acct="$(gcloud config get-value account 2>>"$LOG")"
[ "$acct" = "$EXPECT_ACCOUNT" ] && ok "account $acct" || bad "account is '$acct', expected $EXPECT_ACCOUNT"

orgs="$(gcloud organizations list --format='value(displayName,name)' 2>>"$LOG")"
echo "$orgs" >> "$LOG"
echo "$orgs" | grep -q "^${EXPECT_ORG_DOMAIN}" && ok "organisation $(echo "$orgs" | grep "^${EXPECT_ORG_DOMAIN}" | tr '\t' ' ')" \
  || bad "organisation ${EXPECT_ORG_DOMAIN} not visible"

for p in "tiva-cri-ops-${SUFFIX}" "tiva-cri-dev-${SUFFIX}" "tiva-cri-stg-${SUFFIX}" "tiva-cri-prd-${SUFFIX}"; do
  st="$(gcloud projects describe "$p" --format='value(lifecycleState)' 2>>"$LOG")"
  bill="$(gcloud billing projects describe "$p" --format='value(billingAccountName,billingEnabled)' 2>>"$LOG" | tr '\t' ' ')"
  [ "$st" = ACTIVE ] && ok "project $p ACTIVE; billing: ${bill:-none}" || bad "project $p state '${st:-not found}'"
done
binfo="$(gcloud billing accounts describe "$EXPECT_BILLING" --format='value(displayName,open)' 2>>"$LOG" | tr '\t' ' ')"
[ -n "$binfo" ] && ok "billing account $EXPECT_BILLING: $binfo" || bad "billing account $EXPECT_BILLING not readable"

# No Terraform run in progress: no backend lock object anywhere (old or new buckets).
locks="$(gcloud storage ls "gs://${OPS}-tfstate/**default.tflock" "gs://${OPS}-tfstate-*/**default.tflock" 2>/dev/null)"
[ -z "$locks" ] && ok "no Terraform lock objects (no run in progress)" || bad "lock objects present: $locks"

for env in dev staging production; do
  o="gs://${OPS}-tfstate/cri/${env}/default.tfstate"
  if gcloud storage ls "$o" >/dev/null 2>&1; then
    hold="$(gcloud storage objects describe "$o" --format='value(temporary_hold)' 2>>"$LOG")"
    ok "old state present for ${env} (temporary hold: ${hold:-none})"
  else note "no old state for ${env}"; fi
  b="gs://${OPS}-tfstate-${env}"
  gcloud storage buckets describe "$b" >/dev/null 2>&1 && note "new bucket $b already exists" || note "new bucket $b not yet created"
done

old_sa="terraform@${OPS}.iam.gserviceaccount.com"
d="$(gcloud iam service-accounts describe "$old_sa" --project "$OPS" --format='value(disabled)' 2>>"$LOG")"
ok "shared identity exists (disabled=${d:-False})"
for sa in terraform-dev terraform-staging terraform-prod; do
  gcloud iam service-accounts describe "${sa}@${OPS}.iam.gserviceaccount.com" --project "$OPS" >/dev/null 2>&1 \
    && note "${sa} already exists" || note "${sa} not yet created"
done

[ $fail = 0 ] && echo "PRECHECK PASSED" || echo "PRECHECK FAILED (see $LOG)"
