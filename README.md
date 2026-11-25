# AWS Credits Reporter

AWS Credits Reporter is a self-service FinOps tool for AWS customers. It helps a
customer's own FinOps team manage AWS Organization credits across a
consolidated billing family, using the customer's own read-only credentials in
their management/payer account.

It answers four operational questions:

- **What's new?** Detect newly issued credits since the last run.
- **What's the status?** A dashboard summary of active, inactive, and
  expiring credits.
- **What's expiring?** Proactive alerting on credits that expire this month or
  in the next three months.
- **Who's sharing?** Visibility into which accounts and cost categories
  participate in credit sharing.

Each run retrieves every organization credit from the AWS Billing
`get-credits` API, persists an unmodified timestamped JSON snapshot to S3
(which powers new-credit detection), and builds a plain-text email body: a title naming the payer account, alerts,
a dashboard summary, a sharing breakdown, and a listing of recently expired
credits.
The body can be delivered to the FinOps team by publishing it to an SNS topic
whose subscribers receive it as an email. SNS is the only delivery backend.

There are two ways to run it:

- **CLI** — an on-demand command run manually or from your own scheduler.
- **Scheduled Lambda** — a daily EventBridge-triggered run that publishes its
  outcome to SNS. Deploy it with the self-contained CloudFormation template in
  `infra/`.

## Sample code disclaimer

This is sample code, for non-production usage. You should work with your
security and legal teams to meet your organizational security, regulatory and
compliance requirements before deployment. It is provided under the MIT-0
license, without warranty. Deploying it creates AWS resources (Lambda, SNS, S3,
CloudWatch and, optionally, CloudTrail) that incur charges, and it reads billing
data of your organization, so review the IAM policies in `docs/` before you
grant it access.

## Table of contents

- [Quick install guide (for people and agents)](#quick-install-guide-for-people-and-agents)
- [How it works](#how-it-works)
- [Requirements and permissions](#requirements-and-permissions)
- [Install](#install)
- [CLI usage](#cli-usage)
- [Notifications](#notifications)
- [Snapshots and S3 layout](#snapshots-and-s3-layout)
- [Scheduled deployment (CloudFormation)](#scheduled-deployment-cloudformation)
- [Configuration reference](#configuration-reference)
- [Known limitations and hardening options](#known-limitations-and-hardening-options)
- [Development](#development)
- [Project layout](#project-layout)
- [Contributing, security and license](#contributing-security-and-license)

## Quick install guide (for people and agents)

Run every command from the repository root (the directory that holds this
README). Stop and report if a check fails; do not continue past it.

**1. Pick a path.**

| Path | Use it when | Steps |
|------|-------------|-------|
| CLI | You want an on-demand run, or you will schedule it yourself | 2, 3, 4 |
| Scheduled Lambda | You want a daily run that emails the report through SNS | 2, 3, 5a, 5b, 6 |

**2. Preflight checks (read-only).**

```bash
export AWS_REGION=us-east-1   # the infra/ scripts read AWS_REGION (not AWS_DEFAULT_REGION)
aws --version && python3 --version && pip3 --version && zip -v | head -1
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text) && echo "account: $ACCOUNT_ID"
# Must print the same account id: credits are only returned from the payer account.
aws organizations describe-organization --query Organization.MasterAccountId --output text
```

Stop if the two ids differ, if credentials are missing, or if Python is older
than 3.10. If the stack `aws-credits-reporter` already exists
(`aws cloudformation describe-stacks --stack-name aws-credits-reporter`), the
install below would update it in place; confirm with the user first.

**3. Create a bucket helper** (used for the snapshot bucket on the CLI path and
for the code staging bucket on the Lambda path; encrypted, public access
blocked):

```bash
make_bucket() {  # usage: make_bucket NAME
  if [ "$AWS_REGION" = us-east-1 ]; then
    aws s3api create-bucket --bucket "$1" --region "$AWS_REGION"
  else
    aws s3api create-bucket --bucket "$1" --region "$AWS_REGION" \
      --create-bucket-configuration LocationConstraint="$AWS_REGION"
  fi
  aws s3api put-public-access-block --bucket "$1" --public-access-block-configuration \
    BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
  aws s3api put-bucket-encryption --bucket "$1" --server-side-encryption-configuration \
    '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"}}]}'
}
```

**4. CLI path.**

```bash
SNAP_BUCKET=aws-credits-reporter-snapshots-$ACCOUNT_ID
make_bucket "$SNAP_BUCKET"
python3 -m venv .venv && .venv/bin/pip install -e .
.venv/bin/aws-credits-reporter --bucket "$SNAP_BUCKET"
```

Success looks like: one line per credit, `Total credits: N`,
`Snapshot written: snapshots/<timestamp>.json`, a `Dashboard:` line, and
`Notification skipped`. Exit code 0. Add `--sns-topic-arn <arn>` to send the
report (see [Notifications](#notifications)).

**5. Scheduled Lambda path.** Two roles take part: a *deployer* role that installs
the stack with limited permissions, and the *Lambda execution role* that the
stack creates and caps with a permissions boundary. An administrator sets up the
deployer once (5a); the deployer then runs the install (5b).

*5a. Administrator, once.*

```bash
STAGING_BUCKET=aws-credits-reporter-staging-$ACCOUNT_ID    # code handoff only
SNAP_BUCKET=aws-credits-reporter-snapshots-$ACCOUNT_ID      # created by the stack (or name your existing one)
# Who may assume the deployer role (ask the user; for example the admin or SSO role
# you are signed in with). Replace ASSUMING_ROLE_NAME:
DEPLOYER_PRINCIPAL=arn:aws:iam::${ACCOUNT_ID}:role/ASSUMING_ROLE_NAME
make_bucket "$STAGING_BUCKET"
aws s3api put-bucket-versioning --bucket "$STAGING_BUCKET" \
  --versioning-configuration Status=Enabled
# Keep superseded code versions for a year, then expire them.
aws s3api put-bucket-lifecycle-configuration --bucket "$STAGING_BUCKET" --lifecycle-configuration \
  '{"Rules":[{"ID":"expire-old-code-versions","Status":"Enabled","Filter":{"Prefix":"lambda/"},"NoncurrentVersionExpiration":{"NoncurrentDays":365}}]}'

render() {  # fill the placeholders in a policy file
  sed -e "s/<ACCOUNT_ID>/$ACCOUNT_ID/g" -e "s/<REGION>/$AWS_REGION/g" \
      -e "s/<CODE_STAGING_BUCKET>/$STAGING_BUCKET/g" -e "s/<SNAPSHOT_BUCKET_NAME>/$SNAP_BUCKET/g" "$1"
}
render docs/deployer-permissions-boundary.json > /tmp/aws-credits-reporter-boundary.json
render docs/deployer-iam-policy.json           > /tmp/aws-credits-reporter-deployer.json
# Staging bucket policy: TLS only, and only the deployer role and your administrator
# role may write or delete code zips. Replace ADMIN_ROLE_ARN_PATTERN (for example
# arn:aws:iam::${ACCOUNT_ID}:role/Admin*).
render docs/staging-bucket-policy.json | sed "s#<ADMIN_ROLE_ARN_PATTERN>#$DEPLOYER_PRINCIPAL#" \
  > /tmp/aws-credits-reporter-staging-bucket-policy.json
aws s3api put-bucket-policy --bucket "$STAGING_BUCKET" \
  --policy file:///tmp/aws-credits-reporter-staging-bucket-policy.json
render docs/cloudformation-service-role-policy.json > /tmp/aws-credits-reporter-cfn-service-policy.json
render docs/cloudformation-service-role-trust.json  > /tmp/aws-credits-reporter-cfn-service-trust.json
render docs/cloudformation-service-role-monitoring-policy.json > /tmp/aws-credits-reporter-cfn-service-monitoring-policy.json

aws iam create-policy --policy-name aws-credits-reporter-runtime-boundary \
  --policy-document file:///tmp/aws-credits-reporter-boundary.json
aws iam create-policy --policy-name aws-credits-reporter-deployer \
  --policy-document file:///tmp/aws-credits-reporter-deployer.json

# CloudFormation service role: CloudFormation uses it to create the resources, so the
# deployer needs no Lambda, IAM, SNS or bucket write permissions of its own.
aws iam create-policy --policy-name aws-credits-reporter-cfn-service-policy \
  --policy-document file:///tmp/aws-credits-reporter-cfn-service-policy.json
aws iam create-role --role-name aws-credits-reporter-cfn-service \
  --assume-role-policy-document file:///tmp/aws-credits-reporter-cfn-service-trust.json --max-session-duration 3600
aws iam attach-role-policy --role-name aws-credits-reporter-cfn-service \
  --policy-arn arn:aws:iam::${ACCOUNT_ID}:policy/aws-credits-reporter-cfn-service-policy
# A second managed policy holds the alarm, log group and audit trail permissions
# (one managed policy is limited to 6,144 characters).
aws iam create-policy --policy-name aws-credits-reporter-cfn-service-monitoring-policy \
  --policy-document file:///tmp/aws-credits-reporter-cfn-service-monitoring-policy.json
aws iam attach-role-policy --role-name aws-credits-reporter-cfn-service \
  --policy-arn arn:aws:iam::${ACCOUNT_ID}:policy/aws-credits-reporter-cfn-service-monitoring-policy

# Trust policy: only DEPLOYER_PRINCIPAL, no MFA condition, and only through the global or
# us-east-1 STS endpoint (so every assume is logged where the alert rules watch).
# docs/deployer-trust-policy.json is the same policy with the MFA condition already in it
# (it locks out sign-in methods that do not set aws:MultiFactorAuthPresent,
# so test it first).
cat > /tmp/aws-credits-reporter-trust.json <<TRUST
{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"AWS":"${DEPLOYER_PRINCIPAL}"},"Action":"sts:AssumeRole","Condition":{"StringEquals":{"aws:RequestedRegion":"us-east-1"}}}]}
TRUST
aws iam create-role --role-name aws-credits-reporter-deployer \
  --assume-role-policy-document file:///tmp/aws-credits-reporter-trust.json --max-session-duration 3600
aws iam attach-role-policy --role-name aws-credits-reporter-deployer \
  --policy-arn arn:aws:iam::${ACCOUNT_ID}:policy/aws-credits-reporter-deployer
```

The boundary and the service role must exist before the deploy. The deployer
cannot create, edit or remove either. CloudFormation creates the execution role
with the service role, and the service role can only create it with the boundary
attached.

*5b. Deployer.* Assume the deployer role for this one command and install. The
trust policy accepts only the global or the us-east-1 STS endpoint, so the assume
call carries `--region us-east-1` even when you deploy to another region:

```bash
read -r AK SK ST < <(aws sts assume-role --region us-east-1 --role-session-name install \
  --role-arn arn:aws:iam::${ACCOUNT_ID}:role/aws-credits-reporter-deployer \
  --query 'Credentials.[AccessKeyId,SecretAccessKey,SessionToken]' --output text)
AWS_ACCESS_KEY_ID=$AK AWS_SECRET_ACCESS_KEY=$SK AWS_SESSION_TOKEN=$ST \
PERMISSIONS_BOUNDARY_ARN=arn:aws:iam::${ACCOUNT_ID}:policy/aws-credits-reporter-runtime-boundary \
CFN_SERVICE_ROLE_ARN=arn:aws:iam::${ACCOUNT_ID}:role/aws-credits-reporter-cfn-service \
  infra/install.sh "$STAGING_BUCKET" aws-credits-reporter \
  "SnapshotBucketName=$SNAP_BUCKET TopicManagerPrincipalPattern=$DEPLOYER_PRINCIPAL SnapshotAdminPrincipalPattern=$DEPLOYER_PRINCIPAL"
unset AK SK ST
```

Notes:
- With an administrator identity you can skip 5a and run `infra/install.sh`
  directly. Without `PERMISSIONS_BOUNDARY_ARN` (or `PermissionsBoundaryArn=` in
  the third argument) the script stops with an error, because the Lambda role
  would have no boundary. For a personal or test account where that is the
  intent, set `ALLOW_NO_PERMISSIONS_BOUNDARY=true`; the script then prints a
  warning and goes on.
- `SnapshotAdminPrincipalPattern` names the role that may write and delete snapshots
  by hand. Left empty (the default), the snapshot bucket policy denies deletes to
  everyone, administrators included, so nobody can empty the bucket and deleting the
  stack fails. The example sets it to the same role as `TopicManagerPrincipalPattern`;
  use a separate administrator role if you prefer. A CLI role can write to the
  stack's bucket only if it matches this pattern too.
- To reuse an existing topic or snapshot bucket, add
  `CreateSnsTopic=false ExistingTopicArn=<arn>` and/or
  `CreateSnapshotBucket=false` to the third argument, and set `SNAP_BUCKET` to
  the existing bucket. If the topic is not named `aws-credits-reporter*`, add its
  ARN to the `AllowRuntimeSnsPublish` resource in
  `/tmp/aws-credits-reporter-boundary.json` before creating the policy.
- The third argument is split on spaces, so it cannot carry a value that contains
  spaces (for example `ScheduleExpression`); use the manual deploy under
  [Reuse existing resources](#reuse-existing-resources) for those.

**6. Verify (Lambda path).**

```bash
aws lambda invoke --function-name aws-credits-reporter-lambda --region "$AWS_REGION" \
  --cli-binary-format raw-in-base64-out --payload '{}' /tmp/aws-credits-reporter-out.json
cat /tmp/aws-credits-reporter-out.json                      # "success": true
aws s3 ls "s3://$SNAP_BUCKET/snapshots/" --region "$AWS_REGION"
```

Invoking the function sends a real report to any confirmed subscribers of the
topic. Steps that need a person: confirming the SNS email subscription
(`aws sns subscribe` with the `TopicArn` printed by the install, then the link in
the email), and any administrator-only IAM steps above.

**Update or remove.**
Re-running `infra/install.sh` uploads the current code as a new object version and
pins it in the stack; see [Updating deployed code](#updating-deployed-code).

To remove everything, first empty the snapshot bucket if the stack created it. This
is an administrator step: the deployer cannot read or delete snapshots, and the
bucket policy denies deletes to everyone except `SnapshotAdminPrincipalPattern`, so
the stack must have been deployed with that parameter set to your administrator
role. If it was not, update the stack with `SnapshotAdminPrincipalPattern` set
first, as the deployer. The bucket is versioned, so delete every object version and delete marker.
`aws s3 rm --recursive` is not enough: it only adds delete markers, and the stack
deletion then fails because the bucket is not empty.
```bash
python3 - "$SNAP_BUCKET" <<'PY'
import sys
import boto3
bucket = sys.argv[1]  # your snapshot bucket
s3 = boto3.client("s3")
for page in s3.get_paginator("list_object_versions").paginate(Bucket=bucket):
    objs = [{"Key": o["Key"], "VersionId": o["VersionId"]}
            for o in page.get("Versions", []) + page.get("DeleteMarkers", [])]
    if objs:
        s3.delete_objects(Bucket=bucket, Delete={"Objects": objs, "Quiet": True})
PY
```
If Object Lock is on (`SnapshotObjectLockDays`), the role also needs
`s3:BypassGovernanceRetention` and the `delete_objects` call needs
`BypassGovernanceRetention=True`. Do not pass it for a bucket without Object Lock:
S3 rejects the argument there.

Then run
`aws cloudformation delete-stack --stack-name aws-credits-reporter --role-arn arn:aws:iam::${ACCOUNT_ID}:role/aws-credits-reporter-cfn-service` (the deployer must name the service role) and
`aws cloudformation wait stack-delete-complete --stack-name aws-credits-reporter`.
With the default `CreateLogGroup=true` the stack deletes the function log group. Some
things stay behind and are yours to remove:
- the guardian's log group `/aws/lambda/<FunctionName>-guardian`, if you used the
  guardian (Lambda creates it, the template does not, and it has no retention);
- the audit bucket, if you enabled `EnableSnapshotAuditTrail` (kept on purpose;
  empty it as above and delete it when your retention requirements allow);
- the staging bucket (versioned, so delete all versions as above);
- the IAM resources you created in step 5a (boundary, deployer and service role
  policies and roles, guardian role).

## How it works

A single run is sequenced by the orchestrator:

1. **Retrieve** every organization credit from `get-credits` (paginated, with
   retry/backoff), pinned to `us-east-1` with the payer-account flag.
2. **Retrieve cost categories** from Cost Explorer
   `list-cost-category-definitions` so a credit's `costCategoryArn` can be
   resolved to a category Name.
3. **Persist** the unmodified raw JSON (credits + cost categories) as one
   timestamped S3 snapshot. The snapshot is written before any analysis so the
   data is never lost.
4. **Derive Lifecycle_Status** for each credit at the snapshot date and split
   into Active vs Inactive.
5. **Classify sharing** (sharing disabled when `creditSharingType` is
   `DISABLED`, cost-category-based when a credit has both `costCategoryArn`
   and `ruleName` and no `shareableAccounts`, otherwise shared by default; see
   [Sharing classification](#sharing-classification)). For credits with a cost
   category the category definition is described to find the rule's member
   accounts, account names are looked up in AWS Organizations, and each
   member's credit sharing state is read from the Billing preferences (all
   best-effort). Credits that could not be categorized or resolved are
   collected for the `NEEDS ATTENTION` block.
6. **Detect new credits** by diffing against the most recent prior snapshot.
7. **Classify expiration** tiers over Active credits only (this month / next
   three months).
8. **Build the dashboard** from the lifecycle split, expiration tiers,
   new-credit result, and sharing classification.
9. **Notify** (optional): publish the email body to an SNS topic.

### Lifecycle_Status

The Billing API's `creditStatus` only carries `ENABLED` or `DISABLED`; it is
not a lifecycle. AWS Credits Reporter derives a `Lifecycle_Status` at the snapshot
date, mirroring how the Billing console labels credits. First match wins:

| Status | Description | Rule |
|--------|-------------|------|
| `Expired` | The credit's end date has passed. Any balance left is lost, whether or not the credit was partly used. | `endDate` is present and strictly before the snapshot date |
| `Exhausted` | The credit was fully used before its end date. | `exhaustDate` is present, or `remainingAmount` is zero |
| `Disabled` | The credit is switched off in AWS (shown as `DISABLED` on the credit's detail page in the Billing console), so it can't be used. | `creditStatus` is `DISABLED` |
| `Active` | The credit is valid and can still be used. | none of the above |

Only **Active** credits are considered for expiration alerts. Inactive credits
(Expired / Exhausted / Disabled) are reported separately in the email body:
Paused credits in the `Paused credits` alert, recent Expired and Exhausted
credits in a section at the end.

### Email layout

The email body has these blocks, in this order:

1. The title `AWS CREDITS OVERVIEW FOR PAYER <account id> (<account name>)`,
   always present, followed by a blank line. The name comes from AWS Organizations (or from the Billing
   preferences) and is left out when it cannot be resolved.
2. The alert blocks, each shown only when it has credits: `New credits`,
   `Paused credits`, `Expiring this month`, and `Expiring in the next three
   months`. A credit with a cost category also shows the category, the rule,
   and the member accounts. A member whose credit sharing is off is marked
   `(sharing off)`. A paused credit whose sharing is deactivated for its owning
   account ends with `[credit sharing DEACTIVATED for the owning account <id>]`.
3. `CREDITS DASHBOARD SUMMARY` (see below).
4. `Credits by sharing option` (see [Sharing classification](#sharing-classification)).
5. `NEEDS ATTENTION`, only when credits could not be categorized or resolved.
6. `EXPIRED CREDITS IN THE LAST 3 MONTHS` (or `INACTIVE CREDITS` when recently
   exhausted credits are listed too), only when it has credits to list.

Example:

```
AWS CREDITS OVERVIEW FOR PAYER 123456789012 (Control Tower Management Account)

New credits (2 credits, total remaining $40.00):
- 19000000006  remaining $20.00  ends 2027-03-31
- 19000000007  remaining $20.00  ends 2026-10-31

Paused credits (1 credit, total remaining $20.00):
- 19000000007  Example Promotion for Security Customers  Paused  remaining $20.00  ends 2026-10-31  (cost category "OpenSharing" on rule "FirstOpen" with accounts: Backup Account 555555555555, Log Archive 666666666666 (sharing off))  [credit sharing DEACTIVATED for the owning account 666666666666]

Expiring in the next three months (1 credit, total remaining $20.00):
- 19000000003  remaining $20.00  ends 2026-12-31  (cost category "Credits-Sharing" on rule "Project-Alpha" with accounts: Operations 222222222222, Testing 444455556666)

CREDITS DASHBOARD SUMMARY
- Newly issued credits: 2 credits, total remaining $40.00
...

Credits by sharing option
- cost-category-based:
   - Credits-Sharing (rule Project-Alpha) - 1 credit, total remaining $20.00
       accounts: Operations 222222222222, Testing 444455556666
- Shared by default: 0 credits, total remaining 0.00
- Sharing disabled: 1 credit, total remaining $20.00
   - 19000000006: sharing is off for its account Audit 333333333333
```

### Dashboard summary lines

The `CREDITS DASHBOARD SUMMARY` block in the email has these lines, in this
order. "Remaining balance" means the sum of `remainingAmount` for the credits
counted. Dates and months are in UTC, and "the last 3 months" counts back from
the run date.

```
- Newly issued credits: 2 credits, total remaining $40.00
- Active credits: 3 credits, total remaining $60.00
- Paused: 0 credits, total remaining 0.00
- Exhausted in the last 3 months: 0 credits, total fully used 0.00
- Expiring this month: 1 credit, total remaining $20.00
- Expiring in the next three months: 1 credit, total remaining $20.00
- Expired in the last 3 months: 0 credits, total expired 0.00
```

| Line | Which credits it counts | What the total is |
|------|-------------------------|-------------------|
| **Newly issued credits** | Credits that appear in this run but not in the previous snapshot. Nothing is flagged on the first run (no previous snapshot). If the previous snapshot can't be read, the check is skipped and the email reports a comparison error. | Remaining balance. |
| **Active credits** | Credits that are still usable: not expired, not exhausted, and not disabled. | Remaining balance. |
| **Paused** | Credits switched off in AWS (`creditStatus` is `DISABLED`) that haven't expired or been used up. "Paused" is the email's name for the `Disabled` status. No time limit. | Remaining balance. |
| **Exhausted in the last 3 months** | Credits that were fully used (they have an `exhaustDate`, or nothing remains) within the last 3 months. The date used is `exhaustDate`, or `endDate` when there is none. Credits with neither date are not counted. | Amount used: initial amount minus remaining balance. Credits with no initial amount add nothing. |
| **Expiring this month** | Active credits that end in the current calendar month. | Remaining balance. |
| **Expiring in the next three months** | Active credits that end after this month and by the end of the third following month. A credit is never in both expiring lines. | Remaining balance. |
| **Expired in the last 3 months** | Credits whose end date passed within the last 3 months. Older expirations are left out. | Remaining balance, which is the unused amount lost at expiry. |

Notes:

- Each credit has one status, set by the first matching rule in the
  [Lifecycle_Status](#lifecycle_status) table. A credit that is both fully used
  and past its end date counts as Expired, not Exhausted.
- Only Active credits can appear in the two Expiring lines.
- Paused credits are listed in the `Paused credits` alert at the top of the
  email, one per line.
- Recently Expired and Exhausted credits are listed, one per line, in a section
  at the end of the email. It is titled `EXPIRED CREDITS IN THE LAST 3 MONTHS`
  when every listed credit is Expired, otherwise `INACTIVE CREDITS`. Its
  Expired group uses the same 3-month window. When there is nothing to list
  (for example only older expirations), the section is left out.

### Sharing classification

Each credit goes into exactly one sharing option. The first matching rule wins:

| Order | Condition on the `get-credits` fields | Sharing option | Seen in live data |
|-------|----------------------------------------|----------------|-------------------|
| 1 | `creditSharingType` is `DISABLED` | Sharing disabled | Yes |
| 2 | `shareableAccounts` is not empty | Shared by default | No, it was empty on every credit checked |
| 3 | `costCategoryArn` and `ruleName` are both present | cost-category-based | Yes |
| 4 | anything else, including only one of the two fields | Shared by default | Yes, for credits with no cost category |

How credits are shared in the Billing console, and what the report shows:

- **Default sharing.** A credit is shared with the accounts that have credit
  sharing turned on in the Billing preferences (the credit sharing setting).
- **Cost category sharing.** A credit bound to a cost category rule is shared
  only with the accounts in that rule, and those accounts must also have credit
  sharing turned on. The report lists the rule's accounts and marks each one
  whose credit sharing is off as `(sharing off)`. The state is read from the
  Billing preferences (`aws billing get-billing-preferences --feature CREDIT_SHARING`).
- **Sharing disabled.** When credit sharing is off for the account that owns a
  credit, the API returns `creditSharingType: DISABLED`, whatever the credit's
  binding. The report shows such a credit as "Sharing disabled" and names its
  owning account. If the credit also has a cost category binding, the binding
  details are kept, because the cost category does not make the credit shareable.

Notes from checking a live account:

- The AWS API reference says `costCategoryArn` and `ruleName` are present only
  when `creditSharingType` is `COST_CATEGORY_RULE`. The account checked returned
  them on credits of type `DEFAULT` and `DISABLED`, and never returned
  `COST_CATEGORY_RULE` or `CUSTOM`. Rule 3 therefore keys on the two fields, not
  on the type.
- Rule 2 and the `COST_CATEGORY_RULE` and `CUSTOM` types are covered by unit
  tests with synthetic data only. No credit in the account checked had a
  non-empty `shareableAccounts`.
- The `creditSharingType` of a credit follows its owning account's credit
  sharing preference: deactivating the account changed the credit from
  `DEFAULT` to `DISABLED`, and activating it changed it back. Removing a cost
  category association removed `costCategoryArn` and `ruleName` and nothing else.
- A cost category that is still being applied (`ProcessingStatus` `PROCESSING`)
  is read normally; its rules and accounts are available straight away.
- Only Active credits are counted in the sharing block. A paused credit is shown
  in the `Paused credits` alert instead.

To check your own account, list the fields the tool relies on, and the sharing
state of each account:

```bash
aws billing get-credits --region us-east-1 --account-id <payer-account-id> \
  --payer-account-flag --start-date 2020-01-01 --output table \
  --query 'credits[].{id:creditId,type:creditSharingType,shareable:shareableAccounts,arn:costCategoryArn,rule:ruleName}'

aws billing get-billing-preferences --region us-east-1 --feature CREDIT_SHARING \
  --query 'billingPreferences[?accountId].{account:accountId,name:accountName,sharing:value}' --output table
```

## Requirements and permissions

- **Python 3.10+** for the CLI. The Lambda path runs on the `python3.12`
  runtime.
- The tool **must run in the organization's payer (management) account**.
  `billing:GetCredits` returns the organization's credits only from the payer
  account; from a member account it returns nothing.
- The Billing, STS, Cost Explorer, and Organizations calls are always made in
  **`us-east-1`** regardless of any configured region. `--region` only affects
  the S3 and SNS clients.
- **boto3 1.43 or newer** is required (older releases lack the Billing
  `GetCredits` operation).

The credentials need read-only access to the credit data plus write access to
the snapshot prefix (and SNS only if you send notifications):

| Permission | Why |
|------------|-----|
| `sts:GetCallerIdentity` | Resolve the caller account id (required by `get-credits`) |
| `billing:GetCredits` | Retrieve organization credits |
| `billing:GetBillingPreferences` | Read each account's credit sharing state, to mark cost category members whose sharing is off and to name accounts (optional, best-effort; without it members are shown without marks) |
| `ce:ListCostCategoryDefinitions` | Retrieve cost categories for sharing classification |
| `ce:DescribeCostCategoryDefinition` | Describe a cost category's rules to resolve its member accounts (authoritative sharing) |
| `organizations:ListAccounts` | Resolve account ids to names (optional, best-effort; without it credits show bare account ids) |
| `s3:ListBucket`, `s3:GetObject`, `s3:PutObject` | Read prior snapshots and write the new one (scope to the snapshot prefix) |
| `sns:Publish` | Only if delivering the email body to an SNS topic, and for the scheduled run's outcome topic |

The CloudFormation template in `infra/` provisions an execution role with
exactly these permissions, scoped to the snapshot prefix and the function's own
log group.

If you created the permissions boundary before `billing:GetBillingPreferences`
was added to `docs/deployer-permissions-boundary.json`, an administrator must add
that action to the boundary (a new policy version of
`aws-credits-reporter-runtime-boundary`) and then redeploy the stack. The
boundary caps the role, so until it allows the action the report works but cost
category members are not marked `(sharing off)`, and the run notes that the
credit sharing preferences are unavailable.

## Install

From a virtual environment:

```bash
python3 -m venv .venv
.venv/bin/pip install -e .
```

This installs the `aws-credits-reporter` console script. For development (tests,
property tests, mocked AWS), install the pinned development dependencies
(see [Development](#development)):

```bash
.venv/bin/pip install --require-hashes -r constraints-dev.txt
.venv/bin/pip install --no-deps -e .
```

## CLI usage

The only required argument is the S3 bucket for snapshots. The examples below
derive the bucket name from your account id, as the quick install does:

```bash
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
SNAP_BUCKET=aws-credits-reporter-snapshots-$ACCOUNT_ID
aws-credits-reporter --bucket "$SNAP_BUCKET"
```

That retrieves credits, writes a snapshot, and prints one line per credit, the
totals, the snapshot key, and a one-line dashboard summary. Notifications are
opt-in, so without any notification option it prints `Notification skipped`.

Publish the email body to an SNS topic:

```bash
TOPIC_ARN=$(aws cloudformation describe-stacks --stack-name aws-credits-reporter \
  --region us-east-1 \
  --query "Stacks[0].Outputs[?OutputKey=='TopicArn'].OutputValue" --output text)
aws-credits-reporter --bucket "$SNAP_BUCKET" --sns-topic-arn "$TOPIC_ARN"
```

Use a specific profile and S3 region (Billing still uses `us-east-1`):

```bash
aws-credits-reporter --bucket "$SNAP_BUCKET" --profile payer-readonly --region eu-west-1
```

### CLI options

| Option | Default | Description |
|--------|---------|-------------|
| `--bucket` | *(required)* | S3 bucket that receives the snapshot object. |
| `--prefix` | `snapshots/` | S3 key prefix for snapshots. Include the trailing `/`: the prefix is prepended to the timestamp as is, and snapshots are listed by string prefix. |
| `--expected-bucket-owner` | caller's account | Account id that must own the snapshot bucket. Sent as `ExpectedBucketOwner` on every S3 call, so a bucket name owned by another account is refused. Set it only for a deliberate cross-account bucket. |
| `--expected-topic-owner` | caller's account | Account id that must own the `--sns-topic-arn` topic. A topic in another account is refused. Set it only for a deliberate cross-account topic. |
| `--profile` | *(none)* | AWS CLI profile for credentials. |
| `--region` | *(none)* | Region for the S3 and SNS clients. Billing/STS always use `us-east-1`. |
| `--sns-topic-arn` | *(none)* | SNS topic ARN to publish the email body to. |
| `--dry-run` | off | Prepare the notification but never call SNS. |

**Exit codes:** `0` on success, `1` on a fatal error (setup failure such as
missing credentials, authorization failure, retrieval failure, or snapshot
write failure). Notification problems never
change the exit code: the snapshot already exists, so a delivery failure is
printed as a warning while the run still exits `0`.

## Notifications

Notification is **opt-in**: it runs only when `--sns-topic-arn` or `--dry-run`
is given. Passing either is treated as an explicit request, so `--dry-run`
without `--sns-topic-arn` is reported as a configuration error rather than
silently skipped.

The email body is published to the SNS topic given by `--sns-topic-arn` (see
[CLI usage](#cli-usage)); topic subscribers receive it as an email.

Notes:

- The publish is retried up to three times with exponential backoff under a
  300 second overall deadline. A persistent failure is reported as a warning
  and never changes the exit code.
- `--dry-run` prepares everything (so a missing topic still surfaces) but makes
  no SNS call and needs no SNS permissions.
- The body is plain text, laid out as described in [Email layout](#email-layout):
  the `AWS CREDITS OVERVIEW FOR PAYER` title, the alert blocks (newly issued,
  paused, and expiring credits; present only when there is something to
  report), the `CREDITS DASHBOARD SUMMARY`, the `Credits by sharing option` block, a
  `NEEDS ATTENTION` block (only when credits could not be categorized or
  resolved), and a trailing listing of recently expired credits (only when
  there is one).

## Snapshots and S3 layout

Each run writes one S3 object under the prefix, named with a UTC ISO-8601 basic
timestamp. Snapshots are never overwritten: the write is a conditional put, so
if the key already exists (two runs in the same second) the snapshot is written
as `...Z-1.json`, `...Z-2.json`, and so on, and history is never lost. The object holds the **unmodified** raw
`get-credits` pages and the raw `list-cost-category-definitions` pages from
that run.

New-credit detection compares against a baseline snapshot. Each snapshot
records where its run came from (`"source": "scheduled"` or `"manual"`). Only
the EventBridge schedule's invocation is `scheduled`; CLI runs and any other
Lambda invocation (the console test button, `aws lambda invoke` with `{}`) are
`manual`. The baseline is the newest valid *scheduled* snapshot, so a manual run
reports what is new since the last scheduled run but cannot consume the alert
the next scheduled email is meant to carry. If there is no scheduled snapshot
among the newest 30 (a CLI-only setup, or a fresh deployment), the newest
manual one is used, so the CLI keeps working on its own. Snapshots written
before this field existed count as scheduled. The flag guards against accidental
advances; someone who can invoke the function can still send the schedule's
payload. A baseline that cannot be read is reported as a comparison error but
never fails the run.

## Scheduled deployment (CloudFormation)

`infra/` contains a **self-contained**, plain CloudFormation template (no SAM
transform) that stands the whole solution up in a vanilla account: the IAM
role and least-privilege policy, the Lambda function, a daily EventBridge
schedule, the invoke permission, and (optionally) the SNS topic and snapshot
bucket.

```
infra/
├── cloudformation.yaml        # the self-contained stack
├── package.sh                 # build + upload the code zip, print the deploy command
├── install.sh                 # one-shot: package then deploy, then print outputs
├── requirements-lambda.in     # direct dependency of the Lambda package (boto3)
└── requirements-lambda.txt    # the same, locked with versions and SHA-256 hashes
```

Because CloudFormation can't build or bundle Python dependencies, the code is
packaged into a zip and staged in S3 first (the `infra/package.sh` helper does
this). The zip holds the `aws_credits_reporter` package plus a vendored
`boto3`/`botocore`: the Lambda runtime's bundled copy can lag the AWS API and
may not have the Billing `GetCredits` operation. The vendored dependencies
come from `infra/requirements-lambda.txt`, which pins every package (including
transitive ones) to an exact version and file hashes; `package.sh` installs with
`pip --require-hashes`, so a changed or tampered package fails the build.
`package.sh` packages only the files git tracks in `aws_credits_reporter/` (an untracked file there stops the build, so add a new module to git before packaging), and
it also uploads the zip with a SHA-256 checksum, reads it back, and
`install.sh` pins the stack to the exact S3 object version it uploaded. To
update a dependency, edit `infra/requirements-lambda.in`, regenerate the lock
file with the command in its header, and review the diff.

### Prerequisites

- **AWS credentials for your management (payer) account.** The tool reads
  organization-wide credits, which are only returned from the payer account.
- **AWS CLI**, plus `python3`, `pip3` (23 or newer, for `--require-hashes` with
  `--platform`), `openssl` and `zip` on your PATH (used to build the Lambda zip).
- **`AWS_REGION` set to the deploy region** (the scripts read `AWS_REGION`,
  default `us-east-1`, and ignore `AWS_DEFAULT_REGION` and the profile region).
- **A dedicated S3 bucket you own in the deploy region, with versioning
  enabled,** to stage the code zip (step 5a creates it). This is only a
  handoff location for the Lambda code; it is *not* the snapshot bucket, and it
  is not created for you. Do not reuse a shared bucket such as an AWS Config
  delivery bucket. Versioning is required: `package.sh` stops if the upload
  returns no object version id.

### Deployer permissions (least privilege)

Deploying the stack is more privileged than running the tool: it creates an
IAM role, a Lambda function, an EventBridge rule, and (optionally) an S3 bucket
and SNS topic. That is separate from, and broader than, the read-only role the
Lambda uses at runtime.

A ready-to-attach least-privilege policy for the human/SSO profile that runs
the install is provided at [`docs/deployer-iam-policy.json`](docs/deployer-iam-policy.json),
and the permissions boundary for the Lambda execution role at
[`docs/deployer-permissions-boundary.json`](docs/deployer-permissions-boundary.json).
An administrator creates the boundary as the managed policy
`aws-credits-reporter-runtime-boundary` first, and the deployer policy only
allows creating the execution role with that boundary attached. Before
attaching or creating either, replace the placeholders: `<ACCOUNT_ID>`,
`<REGION>`, `<CODE_STAGING_BUCKET>`, and `<SNAPSHOT_BUCKET_NAME>`. The design,
the audit of every allowed action, and the open questions are in
[`docs/DESIGN.md`](docs/DESIGN.md).

It grants, scoped to the `aws-credits-reporter*` resource names and your two
buckets:

- **CloudFormation** – create/update/delete the stack and its change sets.
- **IAM** – the service role (not the deployer) creates the Lambda execution
  role and its inline policy, but only when the role carries the
  `aws-credits-reporter-runtime-boundary` permissions boundary, and passes it
  to Lambda only. The deployer itself gets one `iam:PassRole`, for the service
  role, to `cloudformation.amazonaws.com` only.
- **Lambda, EventBridge, S3, SNS** – the deployer holds none of these write
  permissions. The CloudFormation service role
  (`docs/cloudformation-service-role-policy.json`) creates the function, the
  daily schedule, the snapshot bucket (with its public-access block,
  encryption, and TLS-only policy) and the notification topic. The deployer
  can only create change sets that name that role (`cloudformation:RoleArn`
  condition), pass it to CloudFormation, and upload the code zip. It cannot
  subscribe recipients to the topic or publish on it; an administrator
  subscribes them (see the quick start) and the topic policy denies everyone
  else.

Two things to know before you deploy:

1. **Run it in the management (payer) account.** These permissions create
   infrastructure; the tool itself only returns credit data when run from the
   payer account, so the deploying profile must be in that account.
2. **The ARNs assume the default names.** The policy is scoped to the stack
   name `aws-credits-reporter` and resource names beginning with
   `aws-credits-reporter`. If you change `FunctionName`, `SnsTopicName`, or
   the stack name off those defaults, widen the matching ARNs in the policy to
   match. `cloudformation:GetTemplateSummary` is scoped to the stack and is
   needed when you re-run the install on an existing stack.

**Validated live** in one account (commercial partition, `us-east-1`): a role
limited to this policy created, updated, re-deployed unchanged, deleted, and
re-created the stack, and every attempted escalation was denied (see
[`docs/DESIGN.md`](docs/DESIGN.md) section 16 for the results and what was not
tested). CloudFormation also probes some read-only actions that the policy does
not grant; those show up as harmless denials in CloudTrail and do not fail the
deploy.
If your deploy reports a real `AccessDenied` in the stack events, add the named
action on the same resource to the service role policy (or, for the change-set
calls themselves, to the deployer policy) and re-run. Security-conscious customers should
prefer this scoped policy; a one-time install from an existing admin/deploy
role also works.

**Reusing an existing SNS topic or snapshot bucket.** The permissions boundary
only allows `sns:Publish` to topics named `aws-credits-reporter*` and S3 access
to the snapshot bucket you put in `<SNAPSHOT_BUCKET_NAME>`. When the stack reuses
a topic with another name (`CreateSnsTopic=false`), add that topic ARN to the
`AllowRuntimeSnsPublish` resource in the boundary before creating it, or the
function's publish is denied.

#### Teardown (deleting the stack)

The deployer policy allows `cloudformation:DeleteStack` only when the call
names the service role (`--role-arn`; remove the action from
`CloudFormationChangeSetAndDeleteWithServiceRoleOnly` if the deployer should
not delete), and the service role holds `s3:DeleteBucket` and
`s3:DeleteBucketPolicy`. Deleting the stack removes the snapshot bucket, which
CloudFormation can only do when the bucket is empty. Emptying it needs
`s3:ListBucket` and `s3:DeleteObject` on the snapshot objects, and the policy
grants no object actions on purpose, so an installer cannot read or delete
snapshot data. Empty the bucket with an admin/operator role before deleting
the stack. The bucket is versioned, so emptying means deleting every object
version and delete marker (the removal steps in the quick install guide include a
snippet), and the bucket policy allows that only to the role matched by
`SnapshotAdminPrincipalPattern`.

### Quick start (vanilla account)

One command packages the code and deploys the whole scheduled stack (IAM role,
Lambda, daily EventBridge schedule, and a newly created SNS topic and snapshot
bucket). Do the steps in this order:

1. An administrator creates the boundary policy from
   `docs/deployer-permissions-boundary.json` (after replacing the placeholders):
   `aws iam create-policy --policy-name aws-credits-reporter-runtime-boundary --policy-document file://docs/deployer-permissions-boundary.json`
2. An administrator attaches `docs/deployer-iam-policy.json` to the deployer
   identity.
3. An administrator creates the CloudFormation service role
   `aws-credits-reporter-cfn-service` from
   `docs/cloudformation-service-role-policy.json` and
   `docs/cloudformation-service-role-trust.json` (step 5a above).
4. The deployer runs the install with `PERMISSIONS_BOUNDARY_ARN` and
   `CFN_SERVICE_ROLE_ARN` set.

The commands use these variables, which the quick install above defines:

- `STAGING_BUCKET` is the version-enabled staging bucket from the prerequisites.
- `SNAP_BUCKET` is a **new, globally unique** S3 bucket name the stack will
  create for the JSON snapshots. (There is no default; you must supply one, so
  the deploy never collides with an existing bucket.) Deriving it from your
  account id keeps it unique and under your control.

```bash
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
STAGING_BUCKET=aws-credits-reporter-staging-$ACCOUNT_ID
SNAP_BUCKET=aws-credits-reporter-snapshots-$ACCOUNT_ID
PERMISSIONS_BOUNDARY_ARN=arn:aws:iam::${ACCOUNT_ID}:policy/aws-credits-reporter-runtime-boundary \
CFN_SERVICE_ROLE_ARN=arn:aws:iam::${ACCOUNT_ID}:role/aws-credits-reporter-cfn-service \
  infra/install.sh "$STAGING_BUCKET" aws-credits-reporter \
  "SnapshotBucketName=$SNAP_BUCKET TopicManagerPrincipalPattern=arn:aws:iam::${ACCOUNT_ID}:role/Admin SnapshotAdminPrincipalPattern=arn:aws:iam::${ACCOUNT_ID}:role/Admin"
```

Without `PERMISSIONS_BOUNDARY_ARN` the script stops with an error. Setting
`ALLOW_NO_PERMISSIONS_BOUNDARY=true` deploys without a boundary on purpose, but
the restricted service role is then denied when the stack creates the IAM role.
Without
`CFN_SERVICE_ROLE_ARN` it prints a warning, and a deployer limited by
`docs/deployer-iam-policy.json` is denied at the change set.

When it finishes it prints the stack outputs, including the SNS topic ARN.
An **administrator** (not the deployer, which is denied `sns:Subscribe`) then
subscribes the address that should receive the daily email. Replace
`<your-email-address>` with your FinOps team's shared mailbox rather than a
personal address. AWS then sends a confirmation email to that address, and the
subscription stays pending, with no reports delivered, until someone clicks the
link in it. The topic policy only lets roles matching the
`TopicManagerPrincipalPattern` stack parameter subscribe, so deploy with it set
to your administrator role (for example
`"SnapshotBucketName=... TopicManagerPrincipalPattern=arn:aws:iam::${ACCOUNT_ID}:role/Admin*"`);
left blank, nobody can subscribe through IAM until the stack is updated.

```bash
aws sns subscribe \
  --topic-arn <TopicArn-from-the-outputs> \
  --protocol email \
  --notification-endpoint <your-email-address>
```

Set `SnapshotAdminPrincipalPattern` to your administrator role as in the example.
Without it nobody can delete snapshots, so the bucket cannot be emptied and the
stack cannot be deleted later (see the removal steps in the quick install guide).
That's the whole install. The report then runs automatically every day at
08:00 UTC (change it with the `ScheduleExpression` parameter).

### Protecting the snapshot bucket

Snapshots decide which credits count as new, so the bucket is protected against
deletion and planting:

- **Bucket owner.** Every S3 call carries `ExpectedBucketOwner` (the caller's
  account by default), and the execution role and the boundary allow S3 access
  only to buckets owned by the stack's account (`aws:ResourceAccount`). A
  look-alike bucket in another account is refused. A cross-account snapshot
  bucket therefore needs `--expected-bucket-owner` for the CLI and edits to the
  role policy and boundary.
- **Versioning, expiry, Object Lock.** A bucket the stack creates has
  versioning, a rule that expires superseded versions after
  `SnapshotNoncurrentRetentionDays`, and optional Object Lock
  (`SnapshotObjectLockDays`).
- **Who can write and delete.** The bucket policy denies `s3:PutObject` under
  the snapshot prefix to everyone except the execution role and
  `SnapshotAdminPrincipalPattern`, and denies object deletes and `DeleteBucket`
  to everyone except that pattern (the CloudFormation service role may also
  delete the bucket).
- **Baseline checks.** Before a snapshot is used as the baseline for new-credit
  detection, the store checks that its key is a snapshot timestamp, that the
  key agrees with `taken_at` inside the document, that it is not in the future,
  and that S3's own `LastModified` is within an hour after `taken_at`. A
  snapshot that fails is skipped and named in the run messages
  (`snapshot not used as baseline: <key> (<reason>)`); the next older one is
  used. These checks do not stop an administrator from writing a plausible
  snapshot.

For a bucket you created yourself (`CreateSnapshotBucket=false`), apply the same
settings by hand: enable versioning, add a lifecycle rule for noncurrent
versions, optionally set an Object Lock default retention, and attach
[`docs/snapshot-bucket-policy.json`](docs/snapshot-bucket-policy.json) after
replacing `<SNAPSHOT_BUCKET_NAME>`, `<ACCOUNT_ID>` and `<ADMIN_ROLE_ARN_PATTERN>`.

### Boundary guardian

The permissions boundary caps the execution role. Service control policies
cannot protect it in the organization's management account (they never apply
there), so with `EnableBoundaryGuardian=true` the stack adds a second small
function, `<FunctionName>-guardian`, that restores the boundary when it is
changed:

- It resets the boundary policy's default version to `ApprovedBoundaryVersion`
  (policy versions are immutable, so this restores the approved document), and
  re-attaches the boundary to the execution role if it was removed or replaced.
- It runs when CloudTrail reports a change to the boundary policy or to the
  role's boundary, and in an hourly sweep. It then alerts on the alarm topic.
  In a live test both kinds of change were undone within about 20 seconds.
- It runs as a role you create, outside the stack, so the deployer and the
  CloudFormation service role cannot edit it:

```bash
sed -e "s/<ACCOUNT_ID>/$ACCOUNT_ID/g" -e "s/<REGION>/$AWS_REGION/g" docs/guardian-role-trust.json > /tmp/guardian-trust.json
# FUNCTION_NAME is the FunctionName parameter of the stack (default below).
sed -e "s/<ACCOUNT_ID>/$ACCOUNT_ID/g" -e "s/<REGION>/$AWS_REGION/g" \
  -e "s/<FUNCTION_NAME>/${FUNCTION_NAME:-aws-credits-reporter-lambda}/g" \
  docs/guardian-role-policy.json > /tmp/guardian-policy.json
aws iam create-role --role-name aws-credits-reporter-guardian \
  --assume-role-policy-document file:///tmp/guardian-trust.json
aws iam put-role-policy --role-name aws-credits-reporter-guardian \
  --policy-name guardian --policy-document file:///tmp/guardian-policy.json
```

  The policy names the execution role, the alarm topic and the guardian's log
  group after `FunctionName`; with a non-default name, set `FUNCTION_NAME` before
  the `sed`, otherwise the guardian cannot read or repair the role or send alerts.
  Then deploy with `EnableBoundaryGuardian=true`,
  `GuardianRoleArn=arn:aws:iam::$ACCOUNT_ID:role/aws-credits-reporter-guardian`
  and `ApprovedBoundaryVersion=<the default version of the boundary policy>`.
- To change the boundary on purpose, create the new version **without** making
  it the default, deploy with the new `ApprovedBoundaryVersion`, then make it the
  default. Otherwise the guardian undoes the change.

The guardian reduces the window; it does not prevent the change. An administrator
can still edit or disable the guardian, and that raises the change alerts.

### Running the CLI with least privilege

The CLI needs far less than administrator rights.
[`docs/cli-role-policy.json`](docs/cli-role-policy.json) allows only: reading
credits, cost categories and account names; `GetObject`, `PutObject` and
`ListBucket` on the snapshot prefix of your own account's bucket; and publishing
to the report topic. Create a role for it, trust the people who run the CLI, and
run with `--profile`. Three things to know:

- With the snapshot bucket policy (see "Protecting the snapshot bucket") only the
  execution role and `SnapshotAdminPrincipalPattern` may write snapshots, so a
  CLI role that should save snapshots has to match that pattern, or the CLI run
  fails at the snapshot write.
- Publishing the report works the same way: the report topic policy denies
  `sns:Publish` to everyone except the execution role and
  `TopicManagerPrincipalPattern`, so the CLI role has to match that pattern too.
  Both patterns are a single ARN pattern with wildcards allowed only after the
  first 4 characters of the role name, so a separate CLI role needs a name that
  shares a prefix with the administrator role (for example `Administrator*`).
  This project's own deployment does not create a separate CLI role (see
  "Known limitations and hardening options"); it runs the CLI as the
  administrator role named in both patterns.
- The CLI refuses a report topic in another account and a snapshot bucket owned
  by another account. `--expected-topic-owner` and `--expected-bucket-owner`
  override this for a deliberate cross-account setup.

### Subscribers and personal data

The only personal data this project holds is the email addresses of the
subscribers on the two SNS topics (the report topic and the alarm topic). The
snapshots hold billing data, not subscriber data.

- Subscribe a shared mailbox or distribution list owned by the FinOps team, not
  personal addresses. People then join and leave through the list, and the
  topic does not accumulate individual addresses.
- Keep a short record of who owns each subscribed address and why it receives
  the report.
- Remove a subscriber with `aws sns list-subscriptions-by-topic --topic-arn
  <arn>` to find the subscription ARN, then `aws sns unsubscribe
  --subscription-arn <arn>`. Deleting the topic removes its subscriptions, but not
  instantly: when testing this sample, a subscription of a deleted topic kept
  delivering for about 10 minutes after a topic with the same name was recreated.
  To remove a recipient on purpose, unsubscribe it first, and wait before you
  recreate a topic under the same name. The topic attribute counters
  (`SubscriptionsConfirmed`, `SubscriptionsPending`) can lag behind the real state;
  trust `list-subscriptions-by-topic`.
  A subscription that was never confirmed expires on its own after 48 hours.
- Retention: an address is kept until it is unsubscribed. Review the
  subscriber list at least once a year and whenever someone leaves the team.
  Only roles matching `TopicManagerPrincipalPattern` can subscribe or
  unsubscribe, so a data subject's request goes to the team that owns that
  role.

### Function logs

With `CreateLogGroup=true` (the default) the stack creates
`/aws/lambda/<FunctionName>` with `LogRetentionDays` (default 90), because the
logs contain account and credit identifiers and exception text. For a log group
that already exists, deploy with `CreateLogGroup=false` and set the retention
yourself: `aws logs put-retention-policy --log-group-name
/aws/lambda/<FunctionName> --retention-in-days 90`.
The code logs run summaries at INFO level, but the Lambda Python runtime's default
logger level is WARNING. In testing, a successful run wrote only the platform lines
(`START`, `END`, `REPORT`) to the log group. Failures are logged at ERROR level
(`handler` in `lambda_handler.py`). To confirm a successful run, check the invocation response, the new
snapshot in S3 and the report email. If you want the INFO lines, set the logger
level in `aws_credits_reporter/lambda_handler.py` (a code change).

### Monitoring and alerts

With `CreateMonitoring=true` (the default) the stack adds a second SNS topic,
`<FunctionName>-alarms`, separate from the report topic so that a broken report
topic still alerts. An administrator subscribes the recipients, the same way as
for the report topic:

```bash
aws sns subscribe --topic-arn <AlarmTopicArn-from-the-outputs> --protocol email \
  --notification-endpoint <your-email-address>
```

What it watches:

| Signal | Detects |
|--------|---------|
| Alarm `<FunctionName>-errors` | the scheduled run failed |
| Alarm `<FunctionName>-throttles` | a second run was rejected while one was running (reserved concurrency is 1) |
| Alarm `<FunctionName>-missing-run` | no invocation for 26 hours: the schedule was deleted or disabled, the permission removed, or the function deleted |
| Alarm `<FunctionName>-missing-report` | no notification was delivered from the report topic for 26 hours: the function could not publish, or a subscription was removed, never confirmed, or given a filter policy that drops the report |
| Rules `<FunctionName>-changes-identity` and `-changes-resources` | privileged changes to this project's function (including creating one), IAM roles and policies, topics and subscriptions (including subscription attributes such as filter policies), buckets, schedule and alarms (including `PutMetricAlarm`), and any use of this project's roles by a caller that is not an AWS service (the alert names the affected function, role, topic, bucket, rule or alarm), such as the deployer, service or guardian role (from CloudTrail management events, IAM and STS events arrive in us-east-1) |
| Lambda failure destination | an invocation that failed after its retries, delivered to the alarm topic |

Deployments through the stack raise change alerts too, which is the expected
signal. The alarm topic accepts publishes from CloudWatch and EventBridge only for
this project's own alarms and `<FunctionName>-changes-*` rules, so another rule in
the account cannot forge or flood alerts there. For behaviors to plan for (no recovery message,
alert volume, failures that send nothing) and a procedure to test the failure
path, see [Operational notes](#operational-notes).

**Audit of snapshot access (optional).** With `EnableSnapshotAuditTrail=true`
the stack also creates a dedicated CloudTrail trail
(`<FunctionName>-snapshot-audit`) that records only S3 data events for the
snapshot prefix (no management events, so nothing is duplicated), writes them
with log file validation to an audit bucket
(a generated name, `aws-credits-reporter-auditlogbucket-<random>`, shown in the stack outputs; encrypted, versioned, expiring
after `AuditLogRetentionDays`, and **kept when the stack is deleted**), and,
with monitoring on, adds the rule `<FunctionName>-changes-snapshot`. That rule
alerts when `GetObject`, `PutObject`, `DeleteObject(s)`, `CopyObject` or
`RestoreObject` on the snapshot bucket is made by anything other than the
execution role, including denied attempts and Administrator use such as a CLI
run. Stopping, deleting or changing the trail also alerts. Data events cost about
0.10 USD per 100,000 events, and CloudTrail delivers log files after about 5
minutes while the alert usually arrives within a minute. `HeadObject` and
`ListObjects` by other principals are logged but do not alert.

### Reuse existing resources

If the SNS topic and snapshot bucket already exist, package the code and deploy
manually, toggling the create parameters off:

```bash
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
STAGING_BUCKET=aws-credits-reporter-staging-$ACCOUNT_ID
# The existing snapshot bucket, for example:
SNAP_BUCKET=aws-credits-reporter-snapshots-$ACCOUNT_ID
# The existing topic ARN, found with: aws sns list-topics --query "Topics[].TopicArn"
TOPIC_ARN=arn:aws:sns:us-east-1:${ACCOUNT_ID}:aws-credits-reporter-topic

infra/package.sh "$STAGING_BUCKET"

aws cloudformation deploy \
  --template-file infra/cloudformation.yaml \
  --stack-name aws-credits-reporter \
  --region us-east-1 \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides \
    CodeS3Bucket="$STAGING_BUCKET" \
    CodeS3ObjectVersion=<version-id-printed-by-package.sh> \
    CreateSnsTopic=false \
    ExistingTopicArn="$TOPIC_ARN" \
    CreateSnapshotBucket=false \
    SnapshotBucketName="$SNAP_BUCKET"
```

`--capabilities CAPABILITY_NAMED_IAM` is required because the stack creates a
named IAM role.

### Updating deployed code

`AWS::Lambda::Function` only picks up new code when its S3 pointer changes.
`install.sh` always uploads with a version-enabled bucket and passes the new
`CodeS3ObjectVersion` to the stack, so re-running it redeploys the current code
and pins exactly the bytes it uploaded. Use a new `CODE_KEY` for each release so
the keys stay readable and old versions are easy to tell apart:

```bash
CODE_KEY=lambda/aws-credits-reporter-2026-11-01.zip \
  PERMISSIONS_BOUNDARY_ARN=arn:aws:iam::${ACCOUNT_ID}:policy/aws-credits-reporter-runtime-boundary \
  CFN_SERVICE_ROLE_ARN=arn:aws:iam::${ACCOUNT_ID}:role/aws-credits-reporter-cfn-service \
  infra/install.sh "$STAGING_BUCKET" aws-credits-reporter \
  "SnapshotBucketName=$SNAP_BUCKET"
```

The deployer policy allows any key that matches
`lambda/aws-credits-reporter*.zip` in the staging bucket, so a new key needs no
policy change. A key outside that pattern needs the policy resource edited.

### Stack parameters

The template groups these in the CloudFormation console wizard (Lambda code,
Function settings, Snapshot S3 bucket, SNS notification topic):

| Parameter | Default | Description |
|-----------|---------|-------------|
| `CodeS3Bucket` | *(required)* | Version-enabled bucket holding the code zip (staging only). |
| `CodeS3Key` | `lambda/aws-credits-reporter.zip` | Key of the code zip. |
| `CodeS3ObjectVersion` | *(empty)* | S3 object version of the code zip. `install.sh` always sets it to the version it just uploaded, so later writes to the key cannot reach the Lambda. Requires bucket versioning. Leave it empty only for a manual deploy you accept to be unpinned. |
| `FunctionName` | `aws-credits-reporter-lambda` | Must start with `aws-credits-reporter` (the Logs policy is scoped to it). |
| `ScheduleExpression` | `cron(0 8 * * ? *)` | Daily run schedule (08:00 UTC). |
| `LambdaMemorySize` | `1024` | Function memory (MB). |
| `LambdaTimeout` | `600` | Function timeout (seconds). |
| `CreateSnapshotBucket` | `true` | `true` creates the snapshot bucket; `false` reuses an existing one. |
| `SnapshotBucketName` | *(required)* | Snapshot bucket name; globally unique when creating. No default — you must supply one. |
| `SnapshotPrefix` | `snapshots/` | Snapshot key prefix (policy + Lambda scoped to it). |
| `SnapshotAdminPrincipalPattern` | *(empty)* | Role ARN pattern (wildcards allowed after the first 4 characters of the role name or path) of administrators who may delete snapshots and write them by hand (for example the CLI). The execution role may always write. Applies to a bucket the stack creates; empty means only the execution role writes and nobody deletes. |
| `SnapshotNoncurrentRetentionDays` | `365` | Days to keep superseded snapshot versions in a stack-created bucket. |
| `SnapshotObjectLockDays` | `0` | Object Lock (GOVERNANCE) default retention in days for a stack-created bucket; `0` is off. While locked, versions cannot be deleted without `s3:BypassGovernanceRetention`, so emptying the bucket before stack deletion waits for the retention to pass or needs that permission (pass `--bypass-governance-retention`). Turning it on for an existing stack-created bucket was an in-place update (no bucket replacement) when tested, and the retention applies to snapshots written afterwards, not to older ones. |
| `CreateSnsTopic` | `true` | `true` creates a topic; `false` reuses `ExistingTopicArn`. |
| `SnsTopicName` | `aws-credits-reporter-topic` | Name of the topic to create. |
| `ExistingTopicArn` | *(empty)* | Topic ARN to reuse when `CreateSnsTopic=false`. |
| `TopicManagerPrincipalPattern` | *(empty)* | Role ARN pattern (wildcards allowed, but the role name or path must start with at least 4 characters that are not `*` or `?`, so `role/*` and `role/?*` are rejected) of the administrators who may subscribe to and publish on the topic the stack creates. The execution role may always publish; everyone else, including the deployer, is denied by the topic policy. Ignored when reusing a topic. |
| `PublishRunOutcome` | `true` | `true` publishes a run-outcome message (JSON) to the topic after every run. `false` skips it for successful runs only, so subscribers get just the report email. Failed runs always publish a failure message. |
| `EnableBoundaryGuardian` | `false` | `true` deploys the boundary guardian function and its rules. Needs `PermissionsBoundaryArn` and `GuardianRoleArn`. See [Boundary guardian](#boundary-guardian). |
| `GuardianRoleArn` | *(empty)* | Role the guardian runs as, created by an administrator outside the stack (`aws-credits-reporter-guardian`). Same pattern rule as the two principal patterns above. |
| `ApprovedBoundaryVersion` | `v1` | Version of the boundary policy that must remain the default. |
| `CreateLogGroup` | `true` | `true` creates the function log group with the retention below and deletes it with the stack; `false` leaves it to Lambda (no expiry) for a group that already exists. See [Function logs](#function-logs). |
| `LogRetentionDays` | `90` | Log retention when the stack creates the log group. |
| `EnableSnapshotAuditTrail` | `false` | `true` creates a dedicated CloudTrail trail for object-level access to the snapshot prefix, its audit bucket and, with monitoring on, an alert on access by any principal except the execution role. See [Monitoring and alerts](#monitoring-and-alerts). |
| `AuditLogRetentionDays` | `365` | Days before audit log files expire. |
| `CreateMonitoring` | `true` | Create the alarm topic, the four CloudWatch alarms, the two change-alert rules and the Lambda failure destination. See [Monitoring and alerts](#monitoring-and-alerts). |
| `PermissionsBoundaryArn` | *(empty)* | IAM managed policy ARN set as the permissions boundary of the execution role. `install.sh` fills it from `PERMISSIONS_BOUNDARY_ARN`. Required when the deployer uses `docs/deployer-iam-policy.json`. |

The scheduled function publishes a run-outcome message (success or failure) to
the topic on every run, and the same topic is used to deliver the email body.
Set `PublishRunOutcome=false` to skip the message for successful runs, so
subscribers get only the report email; failed runs still publish a failure
message either way.

The outcome message and the CLI line `Total remaining` report `total_remaining`,
the sum of `remainingAmount` over every credit in the snapshot, including expired,
exhausted and paused credits. Any balance left on an expired credit is lost, so this
figure is usually larger than the amount you can still use. The dashboard in the
report email counts only Active credits (for example `Active credits: 1 credit,
total remaining $8.16`), so use the dashboard for the usable balance. Retries
are capped: the EventBridge target retries delivery up to 3 times and the
Lambda asynchronous configuration allows 2 retries of a failed invocation.

## Configuration reference

The Lambda handler reads its configuration from environment variables (the
CloudFormation template sets these for you). `AWS_CREDITS_REPORTER_BUCKET` and
`AWS_CREDITS_REPORTER_OUTCOME_TOPIC_ARN` are required; the rest are optional and
mirror the CLI options.

| Variable | Maps to | Description |
|----------|---------|-------------|
| `AWS_CREDITS_REPORTER_BUCKET` | `--bucket` | Snapshot bucket (required). |
| `AWS_CREDITS_REPORTER_OUTCOME_TOPIC_ARN` | *(Lambda only)* | Topic for run-outcome messages (required). |
| `AWS_CREDITS_REPORTER_PREFIX` | `--prefix` | Snapshot key prefix. |
| `AWS_CREDITS_REPORTER_EXPECTED_BUCKET_OWNER` | `--expected-bucket-owner` | Account id that must own the snapshot bucket (default: the function's own account; the template sets it to the stack's account). |
| `AWS_CREDITS_REPORTER_REGION` | `--region` | S3 and SNS client region. |
| `AWS_CREDITS_REPORTER_SNS_TOPIC_ARN` | `--sns-topic-arn` | Email body delivery topic. |
| `AWS_CREDITS_REPORTER_PUBLISH_OUTCOME` | *(Lambda only)* | `true` (default) publishes a run-outcome message (JSON) after every run. `false` skips it for successful runs only. Failed runs always publish a failure message. Any other value is a configuration error. |

## Known limitations and hardening options

This is sample code (see [Sample code disclaimer](#sample-code-disclaimer)). It is
deliberately kept simple to deploy and share, so it makes some design choices that
you may want to change. The table below lists them. None of these items is addressed
by default, and each has an optional way to harden it. Read it before you deploy, and
evaluate each item against your own data classification, governance, compliance and
deployment environment. You decide which ones to harden, and you should test any
change you make.

| Limitation | Design choice | Optional hardening |
|------------|---------------|--------------------|
| **The deployer chooses the template and the zip**. The deployer role cannot change the function, roles or topics directly, but a template or package it submits is created by the CloudFormation service role, and package code runs as the execution role inside the permissions boundary. | Closing it means a second person publishes every release, which adds a step to each deploy. | Two-person rule: an administrator publishes the reviewed template and zip to a prefix the deployer cannot write, and the deployer policy allows `CreateChangeSet` only for that location (`cloudformation:TemplateUrl` condition). Review the diff of `infra/requirements-lambda.txt` and the package on every release. |
| **MFA is not enforced on the deployer role**. | The `aws:MultiFactorAuthPresent` trust condition locks out federated sign-ins that do not set the flag (it did so in testing). | Require MFA where people sign in: your identity provider or IAM Identity Center. Federated sessions (IAM Identity Center, SAML, OIDC) do not carry the flag, so use the condition in `docs/deployer-trust-policy.json` only for IAM users with an MFA device, and test it with a fresh session first. The deployer role is assumable by one named role for one hour, and every use raises an alert. |
| **A snapshot written by an allowed writer is trusted**. | Signing every snapshot needs a KMS key, key policies and a transition rule for older snapshots. | Sign each snapshot with a KMS HMAC key and verify it before use. Today only the execution role and the administrators may write, and baseline checks plus an access alert apply. |
| **Administrators can delete snapshots by bypassing governance retention**. | COMPLIANCE mode cannot be undone for the retention period, which is a heavy default for a shared tool. | Use Object Lock COMPLIANCE mode on the snapshot bucket. |
| **The build runs on the deployer's machine**. | A controlled build pipeline is a project of its own. | Build the zip in a pipeline you control. Dependencies are already pinned by version and hash, so a changed package fails the build. |
| **The report topic (`CreditsTopic`) has no server-side encryption at rest**. The report lists your credits (IDs, names, remaining balances, end dates) and account names. | Whether encryption at rest is required depends on your data classification and governance, and a customer managed key adds cost and a key policy to maintain. | Set `KmsMasterKeyId` on the topic. Use `alias/aws/sns` (the AWS managed key for Amazon SNS), or a customer managed key if your governance or compliance requirements mandate one. With a customer managed key, the execution role needs `kms:GenerateDataKey` and `kms:Decrypt` on it, and the permissions boundary (`docs/deployer-permissions-boundary.json`) must allow them. Tested with `alias/aws/sns` set as `KmsMasterKeyId` on `CreditsTopic` (one added line in the template): publishing and email delivery worked with no change to the execution role or the boundary. The template has no parameter for this, so it is an edit of `infra/cloudformation.yaml`; keep it under the 51,200 byte limit (`docs/DESIGN.md`, section 8.1). |
| **The report email travels in plaintext to the subscriber's mail provider**. | Encrypting the topic does not protect the mailbox, and SNS has no setting that forces TLS to the recipient's mail provider. | Subscribe a shared mailbox with MFA on a domain that enforces inbound TLS (the project assumes this). Treat the report as confidential business data when you choose the recipients. |
| **The alarm topic and the two break-glass topics have no server-side encryption**. | CloudWatch and EventBridge publish to them as AWS services. Encrypting them needs a customer managed key whose key policy allows those service principals, and a wrong key policy stops the alerts from being delivered. | If your policy requires encryption at rest for these topics, use a customer managed key and allow CloudWatch, EventBridge and the Lambda role in its key policy. Send a test alert after the change. |
| **The optional service control policy (SCP) does not protect the customer-managed policies attached to the deployer and CloudFormation service roles**. It protects the runtime permissions boundary policy, and it blocks direct changes to the deployer and service roles. It does not stop `iam:CreatePolicyVersion` or `iam:SetDefaultPolicyVersion` on the policies attached to those roles, so the policy documents remain outside its protection. | The SCP is an optional example, not a complete guardrail set. It is kept small, and the policy names and the principals allowed to change them depend on your organization. | Review the SCP as a starting point and adapt it to your own guardrails. If you need this protection, extend `ProtectBoundaryPolicyFromChange` in `docs/boundary-protection-scp.json` to also cover `aws-credits-reporter-deployer`, `aws-credits-reporter-cfn-service-policy` and `aws-credits-reporter-cfn-service-monitoring-policy`, or restrict those two actions on them in your own controls. Test any SCP change in a non-production account first. |
| **Administrators can still change the boundary, and break-glass use is detected, not prevented**. | Service control policies never apply to the management account, which is where the project runs and where the break-glass caller role lives. The assume alerts are in us-east-1. They would not see a call made through the STS endpoint of another region, so the trust policies of the deployer and break-glass roles accept only the global and the us-east-1 endpoints (`aws:RequestedRegion`; assume these roles with `--region us-east-1`). | The boundary guardian restores the boundary within about 20 seconds and every change alerts. The optional SCP and break-glass role for member accounts, and the alerts for the break-glass roles, are described in `docs/DESIGN.md` (sections 17 and 18). The SCP and the break-glass role are sample code, and the SCP does not cover everything: see the SCP row above. |

This sample does not assess how likely or how serious these limitations are in your
environment. Decide for your organization which ones to harden. The other areas
reviewed while building the sample (topic and snapshot access, baseline tampering,
alerts, the boundary guardian) are covered by the controls described in
`docs/DESIGN.md`. Validate those controls against your own requirements as well.

### Operational notes

Behaviors observed while testing this sample on a payer account. Evaluate each one
against your own operational requirements, and change the template or the code where
it does not fit.
- **Some failures send no message.** The Lambda publishes a failure outcome only
  after its clients exist. A missing required environment variable
  (`ConfigurationError`) or an exception while building the SNS client (for example a
  malformed `AWS_CREDITS_REPORTER_REGION`, which the template never sets) fails the
  invocation without any outcome email. The `<FunctionName>-errors` alarm is the only
  signal for these. Failures after that point publish an outcome: a failure the run
  handles carries the AWS error text, and an unexpected exception carries only its
  type and a generic message (details stay in the log group).
- **No recovery message.** The four alarms define actions for `ALARM` only. When the
  condition clears, the alarm returns to `OK` silently, and the `errors` alarm uses a
  one hour period, so it stays in `ALARM` for up to an hour after the last error. To
  be told about recovery, add `OKActions` with the alarm topic to the alarm resources
  in the template (mind the template size limit in `docs/DESIGN.md`).
- **The missing-run and missing-report alarms fire later than 26 hours.** Both look
  at 26 hourly periods and treat missing data as breaching. In a test with the
  schedule disabled and no deliveries, they entered `ALARM` about 29 to 30 hours after
  the hour of the last invocation and the last delivered report, not after 26, and they
  were still in `ALARM` two days later (no recovery message, and the schedule had not
  been re-enabled). Plan on a day and a half of silence before they alert. The cause of
  the extra delay was not investigated.
- **Alarm history follows the function name.** Lambda metrics are keyed by function
  name. If you delete the stack and redeploy with the same `FunctionName` within an
  hour of an error, the new `errors` alarm can enter `ALARM` right after creation
  because of the old function's datapoint. Wait an hour, or use another name.
- **Alert volume during a deployment.** The change alerts fire once per privileged
  API call and once per assumption of a project role, so an install or update raises
  many emails (tens when the guardian and the audit trail are on). Subscribe the
  alarm topic after the first install, or filter the mail.
- **Who an alert names.** For a deployment through the CloudFormation service role,
  resource changes name that role. If an administrator installs without
  `CFN_SERVICE_ROLE_ARN` (the optional shortcut above), CloudFormation acts with the
  administrator's own permissions and the alerts name that administrator's role.
- **Checking the failure path.** As an administrator (the deployer cannot change the
  function), point the function at a bucket that does not exist, invoke it, and restore
  the original environment:
```bash
FN=aws-credits-reporter-lambda
aws lambda get-function-configuration --function-name "$FN" --query Environment > /tmp/env-original.json
python3 - <<'PY'
import json
env = json.load(open("/tmp/env-original.json"))
env["Variables"]["AWS_CREDITS_REPORTER_BUCKET"] = "amzn-s3-demo-nonexistent-bucket"
json.dump(env, open("/tmp/env-broken.json", "w"))
PY
aws lambda update-function-configuration --function-name "$FN" --environment file:///tmp/env-broken.json
aws lambda wait function-updated --function-name "$FN"
aws lambda invoke --function-name "$FN" --cli-binary-format raw-in-base64-out --payload '{}' /tmp/out.json
# Restore. CloudFormation does not detect this change and will not undo it.
aws lambda update-function-configuration --function-name "$FN" --environment file:///tmp/env-original.json
```
  Expect a "scheduled run FAILED" message on the report topic with the `NoSuchBucket`
  error text, the `errors` alarm in `ALARM` within a few minutes, and change alerts for
  the two environment edits. Subscribers of the report topic see the AWS error text, so
  subscribe only people who may read it.

## Development

```bash
python3 -m venv .venv
.venv/bin/pip install --require-hashes -r constraints-dev.txt
.venv/bin/pip install --no-deps -e .
.venv/bin/python -m pytest -q
```

The test suite (unit, property-based via Hypothesis, and mocked AWS via moto)
runs without any AWS credentials or network access.

[`constraints-dev.txt`](constraints-dev.txt) pins every development dependency
(pytest, Hypothesis, moto, PyYAML and what they pull in) to an exact version with
SHA-256 hashes, so everybody installs the same reviewed packages. The build
backend and the four direct development dependencies are also pinned in
`pyproject.toml` to the same versions (the `boto3` runtime range stays open so the
package works with the SDK version you already use). To update the pins, change
the versions in `pyproject.toml`, run the `uv pip compile` command in the header of
the file and review the diff, because every version change is a supply chain
change. `pip install -e ".[dev]"` installs the same four pinned packages without
hash checking.

## Project layout

```
aws_credits_reporter/
├── cli.py                            # CLI_Runner: argument parsing, output, exit codes
├── lambda_handler.py                 # Deployment_Runner: scheduled Lambda entry point
├── wiring.py                         # Builds real boto3 clients and the Orchestrator (CLI + Lambda)
├── orchestrator.py                   # Sequences one run into a RunResult
├── aws_calls.py                      # Shared AWS error inspection and retry with backoff
├── aggregation.py                    # Shared remaining-amount sums and TierSummary
├── credit_retriever.py               # get-credits (paginated, retry/backoff)
├── category_retriever.py             # list-cost-category-definitions
├── category_definition_retriever.py  # describe-cost-category-definition (rules, members)
├── sharing_preferences.py            # Billing preferences: each account's credit sharing state
├── account_name_resolver.py          # Organizations account id -> name (best-effort)
├── snapshot_store.py                 # S3 write of raw JSON snapshots, read of the prior one
├── models.py                         # Credit / CostCategory parsers, Lifecycle_Status
├── lifecycle_analyzer.py             # Active vs Inactive partition
├── new_credit_detector.py            # Diff against the prior snapshot
├── expiration_analyzer.py            # Expiration risk tiers (Active credits only)
├── sharing_classifier.py             # Sharing option classification
├── attention_builder.py              # Credits that need attention (NEEDS ATTENTION block)
├── dashboard.py                      # Dashboard metrics
├── report_body.py                    # Plain-text email body construction
├── notifier.py                       # SNS delivery with retry
└── guardian.py                       # Boundary guardian Lambda: restores the permissions boundary

infra/                              # CloudFormation template and install scripts
docs/                               # design notes, IAM policies, alert templates, diagrams
tests/                              # unit, property, mocked-AWS and repository file tests
```

## Contributing, security and license

* See [CONTRIBUTING](CONTRIBUTING.md) for how to report bugs, propose changes and submit pull requests. This project follows the [code of conduct](CODE_OF_CONDUCT.md).
* To report a security issue, see [Security issue notifications](CONTRIBUTING.md#security-issue-notifications). Do not open a public issue for it.
* This sample is licensed under the MIT-0 License. See the [LICENSE](LICENSE) file.
