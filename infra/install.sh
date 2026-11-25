#!/usr/bin/env bash
set -euo pipefail

# One-shot installer: package the code and deploy the self-contained stack.
#
# Usage: infra/install.sh <code-staging-bucket> [stack-name] [extra-parameter-overrides]
#
# Deploys the vanilla-account defaults (creates the IAM role + policy, SNS
# topic, and snapshot bucket). To reuse existing resources, pass a third
# argument with extra --parameter-overrides, e.g.:
#   infra/install.sh "$STAGING_BUCKET" aws-credits-reporter \
#     "CreateSnsTopic=false ExistingTopicArn=arn:... CreateSnapshotBucket=false"
#
# Environment:
#   PERMISSIONS_BOUNDARY_ARN  ARN of the IAM managed policy to attach as the
#                             permissions boundary of the execution role, for
#                             example arn:aws:iam::<ACCOUNT_ID>:policy/aws-credits-reporter-runtime-boundary
#                             (see docs/deployer-permissions-boundary.json).
#                             Forwarded as PermissionsBoundaryArn=<arn>. An
#                             explicit PermissionsBoundaryArn= in the third
#                             argument takes precedence. The script refuses to
#                             run when neither is given, because the role would
#                             then have no boundary (an administrator is not
#                             stopped by IAM from doing that).
#   ALLOW_NO_PERMISSIONS_BOUNDARY
#                             Set to "true" to deploy without a boundary on
#                             purpose (a personal or test account). A warning is
#                             still printed. A deployer restricted by
#                             docs/deployer-iam-policy.json is denied at
#                             iam:CreateRole without a boundary regardless.
#   CFN_SERVICE_ROLE_ARN      ARN of the CloudFormation service role (for example
#                             arn:aws:iam::<ACCOUNT_ID>:role/aws-credits-reporter-cfn-service,
#                             see docs/cloudformation-service-role-policy.json).
#                             Forwarded as --role-arn so CloudFormation creates
#                             and updates the resources with that role, not with
#                             the deployer's credentials. A deployer restricted
#                             by docs/deployer-iam-policy.json cannot deploy
#                             without it.

CODE_BUCKET="${1:?usage: install.sh <code-staging-bucket> [stack-name] [extra-parameter-overrides]}"
STACK="${2:-aws-credits-reporter}"
EXTRA_PARAMS="${3:-}"
CODE_KEY="${CODE_KEY:-lambda/aws-credits-reporter.zip}"
REGION="${AWS_REGION:-us-east-1}"
PERMISSIONS_BOUNDARY_ARN="${PERMISSIONS_BOUNDARY_ARN:-}"
ALLOW_NO_PERMISSIONS_BOUNDARY="${ALLOW_NO_PERMISSIONS_BOUNDARY:-}"
CFN_SERVICE_ROLE_ARN="${CFN_SERVICE_ROLE_ARN:-}"

ROOT="$(cd "$(dirname "$0")/.." && pwd)"

# Validate the boundary ARN shape before building or uploading anything.
# Kept in an array so the empty case expands safely under bash 3.2 with set -u.
BOUNDARY_PARAM=()
if [ -n "$PERMISSIONS_BOUNDARY_ARN" ]; then
  case "$PERMISSIONS_BOUNDARY_ARN" in
    arn:aws*:iam::[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]:policy/?*) ;;
    *)
      echo "ERROR: PERMISSIONS_BOUNDARY_ARN is not an IAM policy ARN: $PERMISSIONS_BOUNDARY_ARN" >&2
      exit 1
      ;;
  esac
  BOUNDARY_PARAM=("PermissionsBoundaryArn=$PERMISSIONS_BOUNDARY_ARN")
else
  case " $EXTRA_PARAMS " in
    *" PermissionsBoundaryArn="[!\ ]*) ;;  # given in the third argument
    *)
      if [ "$ALLOW_NO_PERMISSIONS_BOUNDARY" = "true" ]; then
        echo "WARNING: deploying without a permissions boundary (ALLOW_NO_PERMISSIONS_BOUNDARY=true)." >&2
        echo "         The execution role is not capped; see docs/deployer-permissions-boundary.json." >&2
      else
        echo "ERROR: no permissions boundary. Set PERMISSIONS_BOUNDARY_ARN to the boundary policy ARN," >&2
        echo "       or ALLOW_NO_PERMISSIONS_BOUNDARY=true to deploy without one on purpose." >&2
        exit 1
      fi
      ;;
  esac
fi

# Optional CloudFormation service role. Kept in an array so the empty case
# expands safely under bash 3.2 with set -u.
ROLE_ARG=()
if [ -n "$CFN_SERVICE_ROLE_ARN" ]; then
  case "$CFN_SERVICE_ROLE_ARN" in
    arn:aws*:iam::[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]:role/?*) ;;
    *)
      echo "ERROR: CFN_SERVICE_ROLE_ARN is not an IAM role ARN: $CFN_SERVICE_ROLE_ARN" >&2
      exit 1
      ;;
  esac
  ROLE_ARG=(--role-arn "$CFN_SERVICE_ROLE_ARN")
else
  echo "WARNING: CFN_SERVICE_ROLE_ARN is not set; CloudFormation will use the caller's own permissions." >&2
  echo "         A deployer limited by docs/deployer-iam-policy.json is denied without it." >&2
fi

# package.sh builds the zip from pinned, hash-checked dependencies, uploads it
# and reports the S3 object version it stored; the stack is pinned to exactly
# that version, so nothing written to the key afterwards can reach the Lambda.
PACKAGE_RESULT_FILE="$(mktemp)"
trap 'rm -f "$PACKAGE_RESULT_FILE"' EXIT
PACKAGE_RESULT_FILE="$PACKAGE_RESULT_FILE" "$ROOT/infra/package.sh" "$CODE_BUCKET" "$CODE_KEY"
CODE_S3_OBJECT_VERSION="$(sed -n 's/^CODE_S3_OBJECT_VERSION=//p' "$PACKAGE_RESULT_FILE")"
if [ -z "$CODE_S3_OBJECT_VERSION" ]; then
  echo "ERROR: package.sh did not report an object version; refusing to deploy unpinned code." >&2
  exit 1
fi

echo "Deploying stack $STACK ..."
# $EXTRA_PARAMS is deliberately unquoted: the third argument is split on spaces into separate Key=Value overrides (see the README).
# shellcheck disable=SC2086
aws cloudformation deploy \
  --template-file "$ROOT/infra/cloudformation.yaml" \
  --stack-name "$STACK" \
  --region "$REGION" \
  --capabilities CAPABILITY_NAMED_IAM \
  ${ROLE_ARG[@]+"${ROLE_ARG[@]}"} \
  --parameter-overrides CodeS3Bucket="$CODE_BUCKET" CodeS3Key="$CODE_KEY" \
    CodeS3ObjectVersion="$CODE_S3_OBJECT_VERSION" \
    ${BOUNDARY_PARAM[@]+"${BOUNDARY_PARAM[@]}"} $EXTRA_PARAMS


echo "Outputs:"
aws cloudformation describe-stacks --stack-name "$STACK" --region "$REGION" \
  --query 'Stacks[0].Outputs' --output table
