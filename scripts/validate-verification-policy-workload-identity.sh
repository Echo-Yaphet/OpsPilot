#!/usr/bin/env sh
set -eu

evaluation_id=${STAGE11_EVALUATION_ID:?STAGE11_EVALUATION_ID is required}
baseline_revision=${STAGE11_BASELINE_REVISION:-2026091601}
candidate_revision=${STAGE11_CANDIDATE_REVISION:-2026091602}
key_id=opspilot-stage11-local-v1
signing_key=opspilot-stage11-local-signing-key
distribution_token=opspilot-stage11-local-distribution-token
evidence_dir=/evidence/$evaluation_id
host_evidence_dir=work/policy-rollouts/$evaluation_id
nodes_json='{"control-api-primary":"http://control-api:8080","control-api-canary":"http://control-api-canary:8080"}'

restore_default() {
  docker compose --profile active-active up -d --force-recreate --wait \
    workload-identity-issuer policy-distributor control-api control-api-canary >/dev/null
}
trap restore_default EXIT

docker compose --profile policy-rollout build \
  workload-identity-bootstrap workload-identity-issuer \
  policy-distributor policy-rollout-controller control-api control-api-canary
docker compose run --rm --no-deps workload-identity-bootstrap
docker compose up -d --no-deps --force-recreate workload-identity-issuer

docker compose --profile policy-rollout run --rm --no-deps --entrypoint python \
  policy-rollout-controller /app/evaluation.py prepare \
  --evaluation-id "$evaluation_id" \
  --evidence "$evidence_dir" \
  --rollout /rollout \
  --key-id "$key_id" \
  --key "$signing_key" \
  --baseline-revision "$baseline_revision" \
  --candidate-revision "$candidate_revision"

VERIFICATION_POLICY_SIGNING_KEYS="{\"$key_id\":\"$signing_key\"}" \
VERIFICATION_POLICY_REQUIRE_SIGNATURE=true \
VERIFICATION_POLICY_DISTRIBUTION_URL=http://policy-distributor:8070/bundle/stable \
VERIFICATION_POLICY_CANARY_DISTRIBUTION_URL=http://policy-distributor:8070/bundle/canary \
VERIFICATION_POLICY_DISTRIBUTION_TOKEN="$distribution_token" \
VERIFICATION_POLICY_NODE_ID=control-api-primary \
VERIFICATION_POLICY_ROLLOUT_NODES='{"control-api-canary":"http://control-api-canary:8080"}' \
docker compose --profile policy-rollout up -d --force-recreate --wait \
  policy-distributor control-api control-api-canary

VERIFICATION_POLICY_SIGNING_KEYS="{\"$key_id\":\"$signing_key\"}" \
docker compose --profile policy-rollout run --rm --no-deps policy-rollout-controller \
  --candidate "$evidence_dir/candidate.json" \
  --canary-bundle /rollout/canary.json \
  --stable-bundle /rollout/stable.json \
  --nodes-json "$nodes_json" \
  --canary-nodes control-api-canary \
  --quorum 2 \
  --approved \
  --audit-file "$evidence_dir/audit.jsonl" \
  --result-file "$evidence_dir/result.json"

docker compose --profile policy-rollout run --rm --no-deps --entrypoint python \
  policy-rollout-controller /app/evaluation.py verify-identity \
  --evaluation-id "$evaluation_id" \
  --evidence "$evidence_dir"

docker compose --profile policy-rollout run --rm --no-deps --entrypoint python \
  policy-rollout-controller /app/evaluation.py verify \
  --evaluation-id "$evaluation_id" \
  --evidence "$evidence_dir" \
  --candidate-revision "$candidate_revision"

chmod -R a-w "$host_evidence_dir"
