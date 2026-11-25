"""Wiring: build the real boto3 clients and the Orchestrator for a run.

Both entry points (the CLI and the scheduled Lambda) call ``build_orchestrator``
with plain keyword arguments, so neither depends on the other's configuration
format. Client construction is pinned the same way everywhere: Billing, Cost
Explorer, STS, and Organizations use ``us-east-1``; ``region`` only affects the
S3 and SNS clients.
"""

from __future__ import annotations

import time
from typing import Any, Callable

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from aws_credits_reporter.account_name_resolver import (
    AccountNameResolver,
    make_organizations_client,
)
from aws_credits_reporter.category_definition_retriever import CategoryDefinitionRetriever
from aws_credits_reporter.category_retriever import CategoryRetriever, make_cost_explorer_client
from aws_credits_reporter.credit_retriever import (
    CreditRetriever,
    make_billing_client,
    resolve_account_id,
)
from aws_credits_reporter.models import SNAPSHOT_SOURCE_MANUAL
from aws_credits_reporter.notifier import Notifier
from aws_credits_reporter.orchestrator import Orchestrator
from aws_credits_reporter.sharing_preferences import CreditSharingPreferences
from aws_credits_reporter.snapshot_store import SnapshotStore

DEFAULT_PREFIX = "snapshots/"


class SetupError(Exception):
    """Raised when AWS clients or the account id could not be set up."""


def check_topic_owner(sns_topic_arn: str | None, expected_owner: str) -> None:
    """Refuse a report topic that belongs to another account.

    An SNS topic ARN is ``arn:<partition>:sns:<region>:<account>:<name>``. A
    typo or a pasted ARN from another account would otherwise send the report,
    with account ids and amounts, to someone else's topic. An ARN that does not
    have that shape is left for the publish call to reject.
    """
    if not sns_topic_arn:
        return
    parts = sns_topic_arn.split(":")
    if len(parts) < 6 or parts[0] != "arn" or parts[2] != "sns":
        return
    if parts[4] != expected_owner:
        raise SetupError(
            f"the SNS topic belongs to account {parts[4]}, not {expected_owner}; "
            "pass --expected-topic-owner only if that is intended"
        )


def build_notifier(
    sns_topic_arn: str | None,
    dry_run: bool,
    client_factory: Callable[[str], Any],
    sleep: Callable[[float], Any] = time.sleep,
) -> Notifier | None:
    """A ``Notifier`` when notification was requested, else ``None``.

    Notification is opt-in: it is requested by giving a topic ARN or ``dry_run``.
    ``client_factory("sns")`` is not called under ``dry_run``, so a dry run needs
    no SNS access. ``sleep`` is the retry backoff hook (tests inject a no-op).
    """
    if not (sns_topic_arn or dry_run):
        return None
    client = None if dry_run else client_factory("sns")
    return Notifier(client, sns_topic_arn=sns_topic_arn, sleep=sleep, dry_run=dry_run)


def build_orchestrator(
    *,
    bucket: str,
    prefix: str = DEFAULT_PREFIX,
    profile: str | None = None,
    region: str | None = None,
    sns_topic_arn: str | None = None,
    dry_run: bool = False,
    expected_bucket_owner: str | None = None,
    run_source: str = SNAPSHOT_SOURCE_MANUAL,
    expected_topic_owner: str | None = None,
) -> Orchestrator:
    """Construct real boto3 clients and wire the orchestrator.

    Raises ``SetupError`` when credentials are missing or STS rejects the
    caller, so entry points can report a clean message instead of a traceback.

    The Cost Explorer client is shared by the category list and describe calls.
    The Organizations client is best-effort: if it cannot be built, account-name
    resolution degrades to bare account ids rather than failing the run. The
    Billing client is also used, best-effort, to read the credit sharing
    preferences that mark cost category members whose sharing is off.

    ``expected_bucket_owner`` is the account id that must own the snapshot
    bucket; it defaults to the caller's own account and is sent on every S3
    call, so a bucket name that belongs to another account is refused.

    ``expected_topic_owner`` is the account id that must own the report topic;
    it defaults to the caller's own account.

    ``run_source`` flags the snapshot as ``scheduled`` or ``manual`` (default).
    Only the scheduled Lambda passes ``scheduled``; see ``SnapshotStore``.
    """
    session = boto3.Session(profile_name=profile, region_name=region)
    try:
        account_id = resolve_account_id(session)
        billing_client = make_billing_client(session)
        ce_client = make_cost_explorer_client(session)
        s3_client = session.client("s3", region_name=region)
    except ClientError as error:
        detail = error.response.get("Error", {}).get("Message") or str(error)
        raise SetupError(f"could not resolve the caller identity via STS: {detail}") from error
    except BotoCoreError as error:
        raise SetupError(f"could not set up AWS clients: {error}") from error

    check_topic_owner(sns_topic_arn, expected_topic_owner or account_id)

    try:
        org_client = make_organizations_client(session)
    except (BotoCoreError, ClientError):
        org_client = None

    def client_factory(service: str) -> Any:
        try:
            return session.client(service, region_name=region)
        except BotoCoreError as error:
            raise SetupError(f"could not set up the {service} client: {error}") from error

    return Orchestrator(
        credit_retriever=CreditRetriever(billing_client, account_id=account_id),
        snapshot_store=SnapshotStore(
            s3_client,
            bucket=bucket,
            prefix=prefix,
            expected_bucket_owner=expected_bucket_owner or account_id,
        ),
        notifier=build_notifier(sns_topic_arn, dry_run, client_factory),
        category_retriever=CategoryRetriever(ce_client),
        definition_retriever=CategoryDefinitionRetriever(ce_client),
        account_name_resolver=AccountNameResolver(org_client),
        sharing_preferences=CreditSharingPreferences(billing_client),
        payer_account_id=account_id,
        run_source=run_source,
    )
