from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]


def load_workflow(name: str) -> dict:
    return yaml.load((ROOT / ".github" / "workflows" / name).read_text(encoding="utf-8"), Loader=yaml.BaseLoader)


@pytest.mark.contract
def test_build_freshness_policy_and_required_context() -> None:
    workflow = load_workflow("build-guidelines.yml")

    assert "build" in workflow["jobs"]
    assert workflow["on"]["push"]["branches"] == ["main"]
    assert "pull_request" in workflow["on"]
    assert "merge_group" in workflow["on"]
    assert "tags" not in workflow["on"]["push"]
    enforce = workflow["on"]["workflow_call"]["inputs"]["enforce_spec_lock"]
    assert enforce["type"] == "boolean"
    assert enforce["default"] == "false"
    test_job = workflow["jobs"]["fls_audit_tests"]
    test_step = next(step for step in test_job["steps"] if step.get("name") == "Run FLS audit tests")
    assert "uv run --frozen pytest" in test_step["run"]
    assert "tests/unit/fls_audit" in test_step["run"]
    assert "tests/integration/fls_audit" in test_step["run"]
    assert "tests/contract/fls_audit" in test_step["run"]
    build = workflow["jobs"]["build"]
    assert set(build["needs"]) == {"check_rust_examples", "fls_audit_tests"}
    assert build["if"] == "always()"
    prerequisite = next(step for step in build["steps"] if step.get("name") == "Fail if prerequisite checks failed")
    assert "needs.check_rust_examples.result" in prerequisite["if"]
    assert "needs.fls_audit_tests.result" in prerequisite["if"]
    assert "exit 1" in prerequisite["run"]
    assert all(step.get("name") != "Run FLS audit tests" for step in build["steps"])
    build_step = next(step for step in build["steps"] if step.get("name") == "Build documentation")
    assert "inputs.enforce_spec_lock" in build_step["env"]["ENFORCE_SPEC_LOCK"]
    assert "--ignore-spec-lock-diff" in build_step["run"]
    assert "PIPESTATUS[0]" in build_step["run"]
    assert build_step["run"].count("|| true") == 3


@pytest.mark.contract
def test_nightly_and_deploy_enforce_freshness() -> None:
    nightly = load_workflow("nightly.yml")
    deploy = load_workflow("deploy.yml")

    assert nightly["jobs"]["run-build"]["with"]["enforce_spec_lock"] == "true"
    assert deploy["jobs"]["build"]["with"]["enforce_spec_lock"] == "true"
    assert deploy["jobs"]["deploy"]["needs"] == "build"


@pytest.mark.contract
def test_audit_schedule_manual_guard_permissions_and_artifact() -> None:
    workflow = load_workflow("fls-audit.yml")

    assert workflow["on"]["schedule"][0]["cron"] == "0 4 * * *"
    assert "workflow_dispatch" in workflow["on"]
    assert workflow["permissions"] == {"contents": "read", "issues": "write"}
    assert workflow["concurrency"] == {"group": "fls-audit", "cancel-in-progress": "false"}
    steps = workflow["jobs"]["fls-audit"]["steps"]
    guard = next(step for step in steps if step.get("name") == "Require default branch for manual runs")
    assert 'GITHUB_REF" != "refs/heads/$DEFAULT_BRANCH' in guard["run"]
    update = next(step for step in steps if step.get("name") == "Update audit issue")
    assert "--spec-lock src/spec.lock" in update["run"]
    artifact = next(step for step in steps if step.get("name") == "Upload audit reports")
    assert artifact["if"] == "always()"
    assert artifact["with"]["retention-days"] == "90"
