"""A report topic in another account is refused."""

import pytest

from aws_credits_reporter.wiring import SetupError, check_topic_owner

OWNER = "111111111111"


def test_own_account_topic_is_accepted():
    check_topic_owner(f"arn:aws:sns:us-east-1:{OWNER}:aws-credits-reporter-topic", OWNER)


def test_other_account_topic_is_refused():
    with pytest.raises(SetupError, match="222222222222"):
        check_topic_owner("arn:aws:sns:us-east-1:222222222222:topic", OWNER)


def test_explicit_owner_allows_a_deliberate_cross_account_topic():
    check_topic_owner("arn:aws:sns:us-east-1:222222222222:topic", "222222222222")


@pytest.mark.parametrize("value", [None, "", "not-an-arn", "arn:aws:s3:::bucket", "arn:aws:sns:us-east-1"])
def test_other_values_are_left_to_the_publish_call(value):
    check_topic_owner(value, OWNER)
