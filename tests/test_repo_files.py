"""Checks on the files outside the Python package: the CloudFormation template,
the IAM JSON files in docs/, the install scripts and the documentation tables.

No AWS access is needed. The lint tests skip when shellcheck or cfn-lint is not
installed.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
INFRA = ROOT / "infra"
TEMPLATE = INFRA / "cloudformation.yaml"
README = (ROOT / "README.md").read_text(encoding="utf-8")
DESIGN = (DOCS / "DESIGN.md").read_text(encoding="utf-8")

# CloudFormation limit for a template passed in the request body.
TEMPLATE_LIMIT_BYTES = 51_200


class _CfnLoader(yaml.SafeLoader):
    """Safe loader that accepts the short-form tags (!Sub, !If, ...)."""


def _any_tag(loader, _suffix, node):
    if isinstance(node, yaml.ScalarNode):
        return loader.construct_scalar(node)
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node, deep=True)
    return loader.construct_mapping(node, deep=True)


_CfnLoader.add_multi_constructor("!", _any_tag)


def _load_template(path: Path) -> dict:
    # _CfnLoader subclasses yaml.SafeLoader; using it directly avoids yaml.load.
    loader = _CfnLoader(path.read_text(encoding="utf-8"))
    try:
        return loader.get_single_data()
    finally:
        loader.dispose()


def _flatten(value):
    if isinstance(value, (list, tuple)):
        for item in value:
            yield from _flatten(item)
    else:
        yield value


def _first_cells(text: str) -> set[str]:
    """Backticked first cells of markdown table rows: | `Name` | ..."""
    return set(re.findall(r"^\| `([^`]+)` \|", text, flags=re.MULTILINE))


def _allow_actions(statements) -> set[str]:
    actions: set[str] = set()
    for stmt in _flatten(statements):
        if isinstance(stmt, dict) and stmt.get("Effect") == "Allow":
            actions.update(a for a in _flatten(stmt["Action"]) if isinstance(a, str))
    return actions


TEMPLATE_DATA = _load_template(TEMPLATE)
JSON_FILES = sorted(DOCS.glob("*.json"))


# ---------------------------------------------------------------- template size

def test_template_fits_the_request_body_limit():
    size = TEMPLATE.stat().st_size
    assert size <= TEMPLATE_LIMIT_BYTES, (
        f"{TEMPLATE.name} is {size} bytes; the limit is {TEMPLATE_LIMIT_BYTES} "
        "for a template sent in the request body"
    )


# ------------------------------------------------- docs tables vs the template

def test_every_template_parameter_is_in_the_readme_and_design_tables():
    readme_cells, design_cells = _first_cells(README), _first_cells(DESIGN)
    params = set(TEMPLATE_DATA["Parameters"])
    assert not params - readme_cells, f"missing from README: {sorted(params - readme_cells)}"
    assert not params - design_cells, f"missing from DESIGN: {sorted(params - design_cells)}"


def _parameter_table_rows(text: str) -> set[str]:
    """Rows of the table that holds the first template parameter."""
    rows: set[str] = set()
    inside = False
    for line in text.splitlines():
        match = re.match(r"^\| `([^`]+)` \|", line)
        if match and match.group(1) == "FunctionName":
            inside = True
        if inside:
            if match:
                rows.add(match.group(1))
            elif not line.startswith("|"):
                break
    return rows


@pytest.mark.parametrize("name,text", [("README", README), ("DESIGN", DESIGN)])
def test_parameter_tables_hold_no_unknown_parameter(name, text):
    rows = _parameter_table_rows(text)
    assert rows, f"no parameter table found in {name}"
    unknown = rows - set(TEMPLATE_DATA["Parameters"])
    assert not unknown, f"{name} documents parameters the template does not have: {sorted(unknown)}"


@pytest.mark.parametrize("section", ["Resources", "Outputs", "Conditions"])
def test_design_names_every_resource_output_and_condition(section):
    missing = [n for n in TEMPLATE_DATA[section] if f"`{n}`" not in DESIGN]
    assert not missing, f"DESIGN does not mention these {section}: {missing}"


def test_boundary_allows_the_same_actions_as_the_execution_role():
    role = TEMPLATE_DATA["Resources"]["ExecutionRole"]["Properties"]
    template_actions: set[str] = set()
    for policy in role["Policies"]:
        template_actions |= _allow_actions(policy["PolicyDocument"]["Statement"])
    boundary = json.loads((DOCS / "deployer-permissions-boundary.json").read_text())
    assert _allow_actions(boundary["Statement"]) == template_actions


# ------------------------------------------------------------ files and links

def _repo_files() -> list[Path]:
    files = [p for p in list(DOCS.iterdir()) + list(INFRA.iterdir()) if p.is_file()]
    return sorted(p for p in files if p.name != ".DS_Store")


@pytest.mark.parametrize("path", _repo_files(), ids=lambda p: f"{p.parent.name}/{p.name}")
def test_every_docs_and_infra_file_is_referenced(path):
    # A .png or .dot file is a render or the source of a diagram that the text
    # names by its .svg file, so its stem is enough.
    needle = path.stem if path.suffix in {".png", ".dot"} else path.name
    assert needle in README or needle in DESIGN, (
        f"{path.parent.name}/{path.name} is not mentioned in the README or DESIGN"
    )


@pytest.mark.parametrize("name,text", [("README", README), ("DESIGN", DESIGN)])
def test_referenced_docs_and_infra_files_exist(name, text):
    refs = set(re.findall(r"\b((?:docs|infra)/[A-Za-z0-9_.-]*[A-Za-z0-9_])", text))
    missing = sorted(r for r in refs if not (ROOT / r).exists())
    assert not missing, f"{name} points at files that do not exist: {missing}"


@pytest.mark.parametrize("path", JSON_FILES, ids=lambda p: p.name)
def test_docs_json_parses(path):
    assert isinstance(json.loads(path.read_text(encoding="utf-8")), dict)


# ------------------------------------------------------------- trust policies

REGION_CONDITION = {"StringEquals": {"aws:RequestedRegion": "us-east-1"}}


@pytest.mark.parametrize(
    "name",
    ["deployer-trust-policy.json", "breakglass-caller-trust.json", "breakglass-admin-trust.json"],
)
def test_assumable_by_people_only_through_the_us_east_1_sts_endpoint(name):
    policy = json.loads((DOCS / name).read_text())
    for stmt in policy["Statement"]:
        assert stmt["Effect"] == "Allow" and stmt["Action"] == "sts:AssumeRole"
        condition = stmt.get("Condition", {})
        assert condition.get("StringEquals") == REGION_CONDITION["StringEquals"], name


def test_readme_inline_deployer_trust_policy_has_the_region_condition():
    block = re.search(r"<<TRUST\n(.*?)\nTRUST", README, flags=re.DOTALL)
    assert block, "inline deployer trust policy not found in the README"
    assert '"aws:RequestedRegion":"us-east-1"' in block.group(1)


def test_readme_assume_role_call_names_the_region():
    call = re.search(r"aws sts assume-role[^\n]*install", README)
    assert call and "--region us-east-1" in call.group(0)


# --------------------------------------------------------------------- linters

def test_install_scripts_pass_bash_syntax_check():
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash is not installed")
    for script in ("install.sh", "package.sh"):
        result = subprocess.run([bash, "-n", str(INFRA / script)], capture_output=True, text=True)
        assert result.returncode == 0, result.stderr


def test_install_scripts_pass_shellcheck():
    shellcheck = shutil.which("shellcheck")
    if shellcheck is None:
        pytest.skip("shellcheck is not installed")
    result = subprocess.run(
        [shellcheck, "-S", "style", str(INFRA / "install.sh"), str(INFRA / "package.sh")],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize(
    "path,ignore",
    [
        # Two harmless W1030 warnings: parameters that default to an empty string
        # are used only where a condition has checked that they are set.
        (TEMPLATE, ["W1030"]),
        (DOCS / "breakglass-alert.yaml", []),
        (DOCS / "breakglass-caller-alert.yaml", []),
    ],
    ids=lambda v: v.name if isinstance(v, Path) else "ignore",
)
def test_templates_pass_cfn_lint(path, ignore):
    cfn_lint = shutil.which("cfn-lint")
    if cfn_lint is None:
        pytest.skip("cfn-lint is not installed")
    cmd = [cfn_lint, str(path)]
    if ignore:
        cmd += ["--ignore-checks", *ignore]
    result = subprocess.run(cmd, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
