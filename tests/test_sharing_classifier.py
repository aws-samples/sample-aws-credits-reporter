"""Unit tests for Sharing_Classifier (Requirements 14, 18).

The classifier is a pure function over in-memory ``Credit`` and
``CostCategory`` objects, so no AWS or fixtures are needed.
"""

from __future__ import annotations

from aws_credits_reporter.models import CostCategory, Credit
from aws_credits_reporter.sharing_classifier import (
    COST_CATEGORY_BASED,
    SHARED_BY_DEFAULT,
    SharingClassifier,
)

ARN = "arn:aws:ce::111:costcategory/abc"


def test_shareable_accounts_present_is_shared_by_default():
    credit = Credit(creditId="c1", shareableAccounts=["111", "222", "333"])
    result = SharingClassifier().classify([credit], [])
    assert len(result.groups) == 1
    group = result.groups[0]
    assert group.sharing_option == SHARED_BY_DEFAULT
    assert group.cost_category_name is None


def test_shareable_accounts_take_precedence_over_full_binding():
    credit = Credit(
        creditId="c1",
        shareableAccounts=["111"],
        costCategoryArn=ARN,
        ruleName="Credit_Rule",
    )
    result = SharingClassifier().classify(
        [credit], [CostCategory(CostCategoryArn=ARN, Name="Team Sharing")]
    )
    assert result.groups[0].sharing_option == SHARED_BY_DEFAULT


def test_no_binding_is_shared_by_default_even_with_categories():
    credit = Credit(creditId="c1", shareableAccounts=[])
    result = SharingClassifier().classify(
        [credit], [CostCategory(CostCategoryArn=ARN, Name="Team Sharing")]
    )
    group = result.groups[0]
    assert group.sharing_option == SHARED_BY_DEFAULT
    assert group.cost_category_arn is None


def test_arn_without_rule_name_is_shared_by_default():
    credit = Credit(
        creditId="c1", shareableAccounts=[], costCategoryArn=ARN, ruleName=None
    )
    result = SharingClassifier().classify(
        [credit], [CostCategory(CostCategoryArn=ARN, Name="Team Sharing")]
    )
    assert result.groups[0].sharing_option == SHARED_BY_DEFAULT


def test_rule_name_without_arn_is_shared_by_default():
    credit = Credit(
        creditId="c1", shareableAccounts=[], costCategoryArn=None, ruleName="R"
    )
    result = SharingClassifier().classify([credit], [])
    assert result.groups[0].sharing_option == SHARED_BY_DEFAULT


def test_arn_and_rule_name_is_cost_category_based():
    credit = Credit(
        creditId="c1",
        shareableAccounts=[],
        costCategoryArn=ARN,
        ruleName="Credit_Rule",
    )
    result = SharingClassifier().classify(
        [credit], [CostCategory(CostCategoryArn=ARN, Name="Team Sharing")]
    )
    group = result.groups[0]
    assert group.sharing_option == COST_CATEGORY_BASED
    assert group.cost_category_name == "Team Sharing"


def test_group_totality_one_group_per_credit_in_order():
    credits = [
        Credit(creditId="a", shareableAccounts=["1"]),
        Credit(creditId="b", shareableAccounts=[]),
        Credit(
            creditId="c",
            shareableAccounts=[],
            costCategoryArn=ARN,
            ruleName="R",
        ),
    ]
    result = SharingClassifier().classify(
        credits, [CostCategory(CostCategoryArn=ARN, Name="S")]
    )
    assert len(result.groups) == len(credits)
    assert [g.credit.creditId for g in result.groups] == ["a", "b", "c"]
    assert [g.sharing_option for g in result.groups] == [
        SHARED_BY_DEFAULT,
        SHARED_BY_DEFAULT,
        COST_CATEGORY_BASED,
    ]
