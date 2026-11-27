#!/usr/bin/env bash
# Read-only: what the bootstrap needs to know about the organisation.
ORG="${ORG:-930851692765}"
echo "account: $(gcloud config get account 2>/dev/null)"
echo "--- my org roles"
gcloud organizations get-iam-policy "$ORG" --flatten='bindings[].members' \
  --filter="bindings.members:$(gcloud config get account 2>/dev/null)" --format='value(bindings.role)' 2>&1 | tr '\n' ' '; echo
echo "--- projects"; gcloud projects list --format='value(projectId,parent.type,parent.id)' 2>&1 | head
echo "--- enforced org policies"
gcloud org-policies list --organization="$ORG" --format='value(constraint,listPolicy,booleanPolicy)' 2>&1 | sed 's/constraints\///' | head -30
for c in iam.allowedPolicyMemberDomains sql.restrictPublicIp sql.restrictAuthorizedNetworks iam.disableServiceAccountKeyCreation iam.workloadIdentityPoolProviders run.allowedIngress gcp.resourceLocations; do
  printf '%s: ' "$c"; gcloud org-policies describe "$c" --organization="$ORG" --effective --format='value(spec.rules)' 2>&1 | head -c 160; echo
done
