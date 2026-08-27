import json
import os
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]


def workflow_step(workflow_name: str, job_name: str, step_name: str) -> dict:
    workflow = yaml.load(
        (ROOT / ".github" / "workflows" / workflow_name).read_text(encoding="utf-8"),
        Loader=yaml.BaseLoader,
    )
    return next(step for step in workflow["jobs"][job_name]["steps"] if step.get("name") == step_name)


def status(context: str, state: str, created_at: datetime | None = None) -> dict:
    return {
        "context": context,
        "state": state,
        "created_at": (created_at or datetime.now(UTC)).isoformat().replace("+00:00", "Z"),
    }


def run_authorization(tmp_path: Path, statuses: list[dict], *, tag: str = "1.2.3") -> subprocess.CompletedProcess[str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    args_file = tmp_path / "gh-args"
    fake_gh = bin_dir / "gh"
    fake_gh.write_text(
        "#!/usr/bin/env bash\n"
        'printf "%s\\n" "$*" > "$GH_ARGS_FILE"\n'
        'printf "%s" "$STATUS_JSON"\n',
        encoding="utf-8",
    )
    fake_gh.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "GH_ARGS_FILE": str(args_file),
        "STATUS_JSON": json.dumps(statuses),
        "GITHUB_REPOSITORY": "rustfoundation/safety-critical-rust-coding-guidelines",
        "GITHUB_SHA": "a" * 40,
        "GITHUB_REF_NAME": tag,
        "GH_TOKEN": "test-token",
        "PREFLIGHT_MAX_AGE_SECONDS": "86400",
    }
    step = workflow_step("deploy.yml", "authorize-release", "Authorize release")
    result = subprocess.run(
        ["bash", "--noprofile", "--norc", "-e", "-o", "pipefail", "-c", step["run"]],
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    assert f"commits/{'a' * 40}/statuses" in args_file.read_text(encoding="utf-8")
    return result


@pytest.mark.integration
def test_recent_successful_preflight_authorizes_first_publication(tmp_path: Path) -> None:
    result = run_authorization(tmp_path, [status("release-preflight", "success")])

    assert result.returncode == 0
    assert "Recent release preflight authorizes" in result.stdout


@pytest.mark.integration
def test_prior_deployment_authorizes_redeployment(tmp_path: Path) -> None:
    old = datetime.now(UTC) - timedelta(days=30)
    result = run_authorization(
        tmp_path,
        [status("deploy/1.2.3", "success", old), status("release-preflight", "failure")],
    )

    assert result.returncode == 0
    assert "Prior successful deployment authorizes" in result.stdout


@pytest.mark.integration
@pytest.mark.parametrize("state_value", ["pending", "failure", "error"])
def test_non_successful_preflight_rejects_publication(tmp_path: Path, state_value: str) -> None:
    result = run_authorization(tmp_path, [status("release-preflight", state_value)])

    assert result.returncode == 1
    assert f"status is {state_value}, not success" in result.stdout


@pytest.mark.integration
def test_latest_preflight_status_controls_publication(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    result = run_authorization(
        tmp_path,
        [
            status("release-preflight", "pending", now),
            status("release-preflight", "success", now - timedelta(minutes=5)),
        ],
    )

    assert result.returncode == 1
    assert "status is pending, not success" in result.stdout


@pytest.mark.integration
def test_stale_preflight_rejects_first_publication(tmp_path: Path) -> None:
    stale = datetime.now(UTC) - timedelta(hours=25)
    result = run_authorization(tmp_path, [status("release-preflight", "success", stale)])

    assert result.returncode == 1
    assert "outside the 24-hour publication window" in result.stdout


@pytest.mark.integration
def test_deployment_status_for_another_tag_does_not_authorize(tmp_path: Path) -> None:
    result = run_authorization(tmp_path, [status("deploy/2.0.0", "success")])

    assert result.returncode == 1
    assert "No release preflight status exists" in result.stdout


def run_preflight_recorder(
    tmp_path: Path,
    *,
    validate_result: str,
    build_result: str,
) -> tuple[subprocess.CompletedProcess[str], str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    args_file = tmp_path / "gh-args"
    fake_gh = bin_dir / "gh"
    fake_gh.write_text(
        "#!/usr/bin/env bash\n"
        'printf "%s\\n" "$*" > "$GH_ARGS_FILE"\n',
        encoding="utf-8",
    )
    fake_gh.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "GH_ARGS_FILE": str(args_file),
        "GITHUB_REPOSITORY": "rustfoundation/safety-critical-rust-coding-guidelines",
        "GITHUB_SHA": "b" * 40,
        "GITHUB_SERVER_URL": "https://github.com",
        "GITHUB_RUN_ID": "123",
        "GH_TOKEN": "test-token",
        "VALIDATE_RESULT": validate_result,
        "BUILD_RESULT": build_result,
    }
    step = workflow_step("release-preflight.yml", "record", "Record preflight result")
    result = subprocess.run(
        ["bash", "--noprofile", "--norc", "-e", "-o", "pipefail", "-c", step["run"]],
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    return result, args_file.read_text(encoding="utf-8")


@pytest.mark.integration
def test_preflight_recorder_marks_success_only_after_all_jobs_pass(tmp_path: Path) -> None:
    result, args = run_preflight_recorder(tmp_path, validate_result="success", build_result="success")

    assert result.returncode == 0
    assert "state=success" in args
    assert "context=release-preflight" in args


@pytest.mark.integration
@pytest.mark.parametrize(
    ("validate_result", "build_result"),
    [("failure", "skipped"), ("success", "failure"), ("cancelled", "skipped")],
)
def test_preflight_recorder_marks_non_successful_workflow_failed(
    tmp_path: Path,
    validate_result: str,
    build_result: str,
) -> None:
    result, args = run_preflight_recorder(
        tmp_path,
        validate_result=validate_result,
        build_result=build_result,
    )

    assert result.returncode == 1
    assert "state=failure" in args
    assert "context=release-preflight" in args
