"""Boundary guardian.

Service control policies do not apply to an organization's management account,
which is where this project runs, so nothing can prevent an administrator
there from widening the permissions boundary of the execution role. The
guardian is the corrective control: it reconciles two facts and restores them
within seconds, then alerts.

1. The default version of the boundary managed policy must be the approved
   version (``APPROVED_BOUNDARY_VERSION``). Policy versions are immutable, so
   restoring the default pointer restores the approved document exactly.
2. The execution role must carry the approved boundary.

It runs on the EventBridge rule that matches changes to the boundary and on an
hourly sweep, ignores the content of the triggering event, and is idempotent:
its own corrections produce events that find nothing left to fix.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any, Mapping

import boto3
from botocore.exceptions import BotoCoreError, ClientError

logger = logging.getLogger(__name__)

ENV_BOUNDARY_ARN = "BOUNDARY_POLICY_ARN"
ENV_APPROVED_VERSION = "APPROVED_BOUNDARY_VERSION"
ENV_ROLE_NAME = "EXECUTION_ROLE_NAME"
ENV_ALARM_TOPIC_ARN = "ALARM_TOPIC_ARN"


class GuardianConfigurationError(Exception):
    """Raised when a required environment variable is missing."""


@dataclass
class GuardianResult:
    """What one reconcile pass found and did."""

    actions: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        return not self.actions and not self.problems


def _required(env: Mapping[str, str], name: str) -> str:
    value = (env.get(name) or "").strip()
    if not value:
        raise GuardianConfigurationError(f"missing required environment variable {name}")
    return value


def reconcile(
    iam: Any,
    boundary_arn: str,
    approved_version: str,
    role_name: str,
) -> GuardianResult:
    """Bring the boundary policy and the role's boundary back to the approved state."""
    result = GuardianResult()

    # 1. The default version of the boundary policy.
    try:
        policy = iam.get_policy(PolicyArn=boundary_arn)["Policy"]
        current = policy["DefaultVersionId"]
        if current != approved_version:
            # Confirm the approved version still exists before pointing at it.
            iam.get_policy_version(PolicyArn=boundary_arn, VersionId=approved_version)
            iam.set_default_policy_version(PolicyArn=boundary_arn, VersionId=approved_version)
            result.actions.append(
                f"boundary policy default version {current} reset to approved {approved_version}"
            )
    except (ClientError, BotoCoreError, KeyError) as error:
        result.problems.append(f"could not reconcile the boundary policy: {error}")

    # 2. The boundary attached to the execution role.
    try:
        role = iam.get_role(RoleName=role_name)["Role"]
        attached = (role.get("PermissionsBoundary") or {}).get("PermissionsBoundaryArn")
        if attached != boundary_arn:
            iam.put_role_permissions_boundary(
                RoleName=role_name, PermissionsBoundary=boundary_arn
            )
            result.actions.append(
                f"role {role_name} boundary {attached or 'none'} reset to {boundary_arn}"
            )
    except (ClientError, BotoCoreError, KeyError) as error:
        result.problems.append(f"could not reconcile the boundary of {role_name}: {error}")

    return result


def _publish(sns: Any, topic_arn: str, result: GuardianResult) -> None:
    lines = ["AWS Credits Reporter boundary guardian"]
    lines += [f"restored: {action}" for action in result.actions]
    lines += [f"problem: {problem}" for problem in result.problems]
    try:
        sns.publish(
            TopicArn=topic_arn,
            Subject="AWS Credits Reporter: permissions boundary restored"
            if result.actions
            else "AWS Credits Reporter: boundary guardian problem",
            Message="\n".join(lines),
        )
    except (ClientError, BotoCoreError):
        logger.exception("could not publish the guardian alert")


def handler(
    event: Any,
    context: Any,
    iam_client: Any = None,
    sns_client: Any = None,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Lambda entry point: reconcile once and alert when something was restored."""
    env = os.environ if env is None else env
    boundary_arn = _required(env, ENV_BOUNDARY_ARN)
    approved_version = _required(env, ENV_APPROVED_VERSION)
    role_name = _required(env, ENV_ROLE_NAME)
    topic_arn = (env.get(ENV_ALARM_TOPIC_ARN) or "").strip()

    iam = iam_client if iam_client is not None else boto3.client("iam")
    result = reconcile(iam, boundary_arn, approved_version, role_name)

    if not result.clean:
        logger.warning("guardian: actions=%s problems=%s", result.actions, result.problems)
        if topic_arn:
            sns = sns_client if sns_client is not None else boto3.client("sns")
            _publish(sns, topic_arn, result)
    else:
        logger.info("guardian: boundary matches the approved state")
    return {"actions": result.actions, "problems": result.problems}
