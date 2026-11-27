#!/usr/bin/env bash
# Usage: expect_denied.sh "<description>" <command...>
#
# Isolation probe. Passes ONLY when the command fails with an authorisation
# denial (HTTP 403 / PERMISSION_DENIED / missing permission). Any other outcome
# fails the probe:
#   * success                      -> isolation is broken
#   * failure for another reason   -> inconclusive (network, missing API,
#                                     wrong resource name, quota, 404, ...)
# so an unrelated failure can never be mistaken for proof of isolation.
set -uo pipefail
desc="$1"; shift
err="$(mktemp)"; trap 'rm -f "$err"' EXIT
if "$@" >/dev/null 2>"$err"; then
  echo "FAIL: ${desc}: the command succeeded (isolation broken)"; exit 1
fi
if grep -Eiq 'PERMISSION_DENIED|HTTPError 403|\b403\b|does not have [a-z.]+ (access|permission)|AccessDenied|permission[^ ]* denied|Forbidden' "$err"; then
  echo "ok: ${desc}: denied ($(grep -Eio 'PERMISSION_DENIED|403|Forbidden|AccessDenied|does not have [a-z.]+ (access|permission)' "$err" | head -1))"
  exit 0
fi
echo "FAIL: ${desc}: failed for a reason other than permission denial (inconclusive):"
sed -e 's/^/    /' "$err" | head -5
exit 1
