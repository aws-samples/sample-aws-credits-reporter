#!/usr/bin/env bash
set -euo pipefail

# Build and upload the AWS Credits Reporter Lambda deployment package to a staging
# bucket, then print the deploy command.
#
# Usage: infra/package.sh <code-staging-bucket> [code-s3-key]
#
# <code-staging-bucket> must already exist and be in the same region you
# deploy the stack into. It is only used to hand the code zip to CloudFormation;
# it is independent of the snapshot bucket the stack manages. It must have
# versioning enabled so the stack can pin the uploaded object version.

CODE_BUCKET="${1:?usage: package.sh <code-staging-bucket> [code-s3-key]}"
CODE_KEY="${2:-lambda/aws-credits-reporter.zip}"
REGION="${AWS_REGION:-us-east-1}"

ROOT="$(cd "$(dirname "$0")/.." && pwd)"

# Fail early with a clear message if the staging bucket is missing or not
# accessible, rather than after building the whole zip.
aws s3api head-bucket --bucket "$CODE_BUCKET" --region "$REGION" 2>/dev/null \
  || { echo "ERROR: staging bucket '$CODE_BUCKET' not found or not accessible in $REGION" >&2; exit 1; }

BUILD="$(mktemp -d)"
trap 'rm -rf "$BUILD"' EXIT

echo "Staging package in $BUILD"

# aws_credits_reporter is the only first-party code. Only files that git tracks
# go into the zip, so a stray or untracked file in the directory (a scratch
# script, a local secret) is never shipped. Untracked files stop the build: a
# new module that was never added would otherwise be missing at runtime.
# Outside a git work tree (for example a downloaded archive) only *.py files
# are copied.
mkdir -p "$BUILD/aws_credits_reporter"
if git -C "$ROOT" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  UNTRACKED="$(git -C "$ROOT" ls-files --others --exclude-standard -- aws_credits_reporter)"
  if [ -n "$UNTRACKED" ]; then
    echo "ERROR: untracked files in aws_credits_reporter/ would not be packaged. Add them to git or remove them:" >&2
    # shellcheck disable=SC2001  # a prefix on every line of a multi-line list; parameter expansion cannot do that
    echo "$UNTRACKED" | sed 's/^/         /' >&2
    exit 1
  fi
  if [ -n "$(git -C "$ROOT" diff --name-only HEAD -- aws_credits_reporter)" ]; then
    echo "WARNING: aws_credits_reporter/ has uncommitted changes; the package contains the working tree, not HEAD." >&2
  fi
  git -C "$ROOT" ls-files -z -- aws_credits_reporter | while IFS= read -r -d '' f; do
    [ -f "$ROOT/$f" ] || continue
    mkdir -p "$BUILD/$(dirname "$f")"
    cp "$ROOT/$f" "$BUILD/$f"
  done
else
  echo "NOTE: not a git work tree; packaging only *.py files from aws_credits_reporter/" >&2
  ( cd "$ROOT" && find aws_credits_reporter -type f -name '*.py' -not -path '*/__pycache__/*' -print0 ) \
    | while IFS= read -r -d '' f; do
        mkdir -p "$BUILD/$(dirname "$f")"
        cp "$ROOT/$f" "$BUILD/$f"
      done
fi
if [ ! -f "$BUILD/aws_credits_reporter/lambda_handler.py" ]; then
  echo "ERROR: aws_credits_reporter/lambda_handler.py is missing from the package." >&2
  exit 1
fi

# The Lambda python3.12 runtime bundles boto3/botocore, but that copy can lag
# the AWS API: the Billing GetCredits operation is only in botocore >= 1.43, so
# the runtime's older model raises "'Billing' object has no attribute
# 'get_credits'". Bundle a current boto3/botocore into the zip so it shadows
# the runtime copy. Linux wheels are fetched explicitly so the package works on
# the Lambda platform regardless of the host OS building it.
#
# Supply chain: infra/requirements-lambda.txt pins every
# dependency, including the transitive ones, to an exact version and the SHA-256
# of its files. --require-hashes makes pip fail if any downloaded file differs,
# and there is no --upgrade, so a compromised newer release or a tampered
# package index cannot change what gets vendored.
PIP="${PIP:-pip3}"
echo "Vendoring pinned, hash-checked dependencies (Linux, py3.12 wheels) into the package"
"$PIP" install \
  --platform manylinux2014_x86_64 \
  --python-version 3.12 \
  --implementation cp \
  --only-binary=:all: \
  --require-hashes \
  --no-deps \
  --target "$BUILD" \
  -r "$ROOT/infra/requirements-lambda.txt" >/dev/null

find "$BUILD" -type d -name '__pycache__' -prune -exec rm -rf {} +
# Trim test/dist metadata to keep the zip lean.
find "$BUILD" -type d -name '*.dist-info' -prune -exec rm -rf {} + 2>/dev/null || true

ZIP="$(mktemp -d)/aws-credits-reporter.zip"
trap 'rm -rf "$BUILD" "$(dirname "$ZIP")"' EXIT
( cd "$BUILD" && zip -r -q "$ZIP" . -x '*.pyc' )

# SHA-256 of the zip, hex (for people) and base64 (S3 checksum format).
ZIP_SHA256_HEX="$(openssl dgst -sha256 "$ZIP" | awk '{print $NF}')"
ZIP_SHA256_B64="$(openssl dgst -sha256 -binary "$ZIP" | base64)"
echo "Package SHA-256: $ZIP_SHA256_HEX"

# Upload with an S3 checksum so S3 rejects a corrupted upload, and keep the
# object version id so the stack can pin the exact bytes uploaded. The staging bucket must have versioning enabled; without it S3 returns
# no version id and the upload is treated as an error.
echo "Uploading to s3://$CODE_BUCKET/$CODE_KEY (region $REGION)"
OBJECT_VERSION="$(aws s3api put-object \
  --bucket "$CODE_BUCKET" --key "$CODE_KEY" --body "$ZIP" \
  --checksum-sha256 "$ZIP_SHA256_B64" \
  --region "$REGION" --query 'VersionId' --output text)"
case "$OBJECT_VERSION" in
  ""|None|null)
    echo "ERROR: the upload returned no object version id. Enable versioning on '$CODE_BUCKET' and retry." >&2
    exit 1
    ;;
esac

# Read back what S3 stored for that exact version and compare checksums.
STORED_SHA256_B64="$(aws s3api head-object \
  --bucket "$CODE_BUCKET" --key "$CODE_KEY" --version-id "$OBJECT_VERSION" \
  --checksum-mode ENABLED --region "$REGION" --query 'ChecksumSHA256' --output text)"
if [ "$STORED_SHA256_B64" != "$ZIP_SHA256_B64" ]; then
  echo "ERROR: checksum of s3://$CODE_BUCKET/$CODE_KEY (version $OBJECT_VERSION) does not match the built package." >&2
  exit 1
fi
echo "Uploaded version: $OBJECT_VERSION (checksum verified)"

# install.sh reads these to pin the stack to this exact object version.
if [ -n "${PACKAGE_RESULT_FILE:-}" ]; then
  {
    echo "CODE_S3_OBJECT_VERSION=$OBJECT_VERSION"
    echo "CODE_SHA256=$ZIP_SHA256_HEX"
  } > "$PACKAGE_RESULT_FILE"
fi

# <boundary-arn> below is the execution role permissions boundary (see
# docs/deployer-permissions-boundary.json); install.sh forwards it from the
# PERMISSIONS_BOUNDARY_ARN environment variable.
cat <<EOF

Package uploaded. Deploy the self-contained stack with:

  aws cloudformation deploy \\
    --template-file infra/cloudformation.yaml \\
    --stack-name aws-credits-reporter \\
    --region $REGION \\
    --capabilities CAPABILITY_NAMED_IAM \\
    --parameter-overrides \\
      CodeS3Bucket=$CODE_BUCKET \\
      CodeS3Key=$CODE_KEY \\
      CodeS3ObjectVersion=$OBJECT_VERSION \\
      PermissionsBoundaryArn=<boundary-arn>

Vanilla account (creates role, policy, SNS topic, and snapshot bucket) is the
default. To reuse an existing topic or bucket, add:
      CreateSnsTopic=false ExistingTopicArn=<arn>
      CreateSnapshotBucket=false
EOF
