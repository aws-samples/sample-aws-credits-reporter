"""Boundary guardian: restores the approved boundary state."""

from __future__ import annotations

import pytest
from botocore.exceptions import ClientError

from aws_credits_reporter.guardian import (
    ENV_ALARM_TOPIC_ARN,
    ENV_APPROVED_VERSION,
    ENV_BOUNDARY_ARN,
    ENV_ROLE_NAME,
    GuardianConfigurationError,
    handler,
    reconcile,
)

BOUNDARY = "arn:aws:iam::111111111111:policy/aws-credits-reporter-runtime-boundary"
ROLE = "aws-credits-reporter-lambda-role"
TOPIC = "arn:aws:sns:us-east-1:111111111111:aws-credits-reporter-lambda-alarms"


class FakeIam:
    def __init__(self, default="v3", role_boundary=BOUNDARY, versions=("v1", "v2", "v3"), fail=None):
        self.default = default
        self.role_boundary = role_boundary
        self.versions = set(versions)
        self.fail = fail or {}
        self.calls: list[tuple[str, dict]] = []

    def _maybe_fail(self, name):
        if name in self.fail:
            raise ClientError({"Error": {"Code": self.fail[name], "Message": "x"}}, name)

    def get_policy(self, **kwargs):
        self._maybe_fail("get_policy")
        return {"Policy": {"DefaultVersionId": self.default}}

    def get_policy_version(self, **kwargs):
        if kwargs["VersionId"] not in self.versions:
            raise ClientError({"Error": {"Code": "NoSuchEntity", "Message": "x"}}, "GetPolicyVersion")
        return {}

    def set_default_policy_version(self, **kwargs):
        self._maybe_fail("set_default_policy_version")
        self.calls.append(("set_default_policy_version", kwargs))
        self.default = kwargs["VersionId"]

    def get_role(self, **kwargs):
        boundary = {"PermissionsBoundaryArn": self.role_boundary} if self.role_boundary else None
        role = {"RoleName": kwargs["RoleName"]}
        if boundary:
            role["PermissionsBoundary"] = boundary
        return {"Role": role}

    def put_role_permissions_boundary(self, **kwargs):
        self.calls.append(("put_role_permissions_boundary", kwargs))
        self.role_boundary = kwargs["PermissionsBoundary"]


class FakeSns:
    def __init__(self):
        self.published = []

    def publish(self, **kwargs):
        self.published.append(kwargs)
        return {"MessageId": "1"}


def test_nothing_to_do_when_state_is_approved():
    iam = FakeIam()
    result = reconcile(iam, BOUNDARY, "v3", ROLE)
    assert result.clean and iam.calls == []


def test_default_version_is_reset():
    iam = FakeIam(default="v4", versions=("v1", "v2", "v3", "v4"))
    result = reconcile(iam, BOUNDARY, "v3", ROLE)
    assert iam.default == "v3"
    assert any("reset to approved v3" in a for a in result.actions)


def test_missing_role_boundary_is_reattached():
    iam = FakeIam(role_boundary=None)
    result = reconcile(iam, BOUNDARY, "v3", ROLE)
    assert iam.role_boundary == BOUNDARY
    assert any("none" in a for a in result.actions)


def test_wrong_role_boundary_is_replaced():
    iam = FakeIam(role_boundary="arn:aws:iam::111111111111:policy/other")
    reconcile(iam, BOUNDARY, "v3", ROLE)
    assert iam.role_boundary == BOUNDARY


def test_missing_approved_version_is_a_problem_not_a_change():
    iam = FakeIam(default="v4", versions=("v4",))
    result = reconcile(iam, BOUNDARY, "v3", ROLE)
    assert iam.default == "v4"
    assert result.problems and not result.actions


def test_api_failure_is_reported_and_the_other_check_still_runs():
    iam = FakeIam(role_boundary=None, fail={"get_policy": "AccessDenied"})
    result = reconcile(iam, BOUNDARY, "v3", ROLE)
    assert result.problems
    assert iam.role_boundary == BOUNDARY


def _env(**extra):
    env = {
        ENV_BOUNDARY_ARN: BOUNDARY,
        ENV_APPROVED_VERSION: "v3",
        ENV_ROLE_NAME: ROLE,
        ENV_ALARM_TOPIC_ARN: TOPIC,
    }
    env.update(extra)
    return env


def test_handler_alerts_when_it_restores():
    iam, sns = FakeIam(default="v4", versions=("v1", "v3", "v4")), FakeSns()
    out = handler({}, None, iam_client=iam, sns_client=sns, env=_env())
    assert out["actions"] and len(sns.published) == 1
    assert sns.published[0]["TopicArn"] == TOPIC
    assert "restored" in sns.published[0]["Message"]


def test_handler_is_quiet_when_clean():
    sns = FakeSns()
    out = handler({}, None, iam_client=FakeIam(), sns_client=sns, env=_env())
    assert out == {"actions": [], "problems": []} and sns.published == []


def test_handler_without_topic_does_not_publish():
    sns = FakeSns()
    handler({}, None, iam_client=FakeIam(role_boundary=None), sns_client=sns, env=_env(**{ENV_ALARM_TOPIC_ARN: ""}))
    assert sns.published == []


@pytest.mark.parametrize("missing", [ENV_BOUNDARY_ARN, ENV_APPROVED_VERSION, ENV_ROLE_NAME])
def test_handler_requires_configuration(missing):
    env = _env()
    del env[missing]
    with pytest.raises(GuardianConfigurationError):
        handler({}, None, iam_client=FakeIam(), sns_client=FakeSns(), env=env)
