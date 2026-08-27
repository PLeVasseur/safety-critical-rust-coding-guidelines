import os
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "scripts" / "fls_release_status.sh"


def workflow_step(workflow_name: str, job_name: str, step_name: str) -> dict:
    workflow = yaml.load(
        (ROOT / ".github" / "workflows" / workflow_name).read_text(encoding="utf-8"),
        Loader=yaml.BaseLoader,
    )
    return next(step for step in workflow["jobs"][job_name]["steps"] if step.get("name") == step_name)


def status_row(context: str, state: str, created_at: datetime | None = None) -> str:
    timestamp = (created_at or datetime.now(UTC)).isoformat().replace("+00:00", "Z")
    return f"{context}\t{state}\t{timestamp}"


def run_release_script(
    tmp_path: Path,
    command: str,
    status_rows: list[str] | None = None,
    *,
    tag: str = "1.2.3",
    extra_env: dict[str, str] | None = None,
) -> tuple[subprocess.CompletedProcess[str], str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    args_file = tmp_path / "gh-args"
    fake_gh = bin_dir / "gh"
    fake_gh.write_text(
        "#!/usr/bin/env bash\n"
        'printf "%s\\n" "$*" >> "$GH_ARGS_FILE"\n'
        'if [[ "$*" == *"--method GET"* ]]; then\n'
        '  printf "%s" "$STATUS_ROWS"\n'
        "fi\n",
        encoding="utf-8",
    )
    fake_gh.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "GH_ARGS_FILE": str(args_file),
        "STATUS_ROWS": "\n".join(status_rows or []),
        "GITHUB_REPOSITORY": "rustfoundation/safety-critical-rust-coding-guidelines",
        "GITHUB_SHA": "a" * 40,
        "GITHUB_REF_NAME": tag,
        "GITHUB_SERVER_URL": "https://github.com",
        "GITHUB_RUN_ID": "123",
        "GH_TOKEN": "test-token",
        "PREFLIGHT_MAX_AGE_SECONDS": "86400",
        "PREFLIGHT_FUTURE_TOLERANCE_SECONDS": "300",
        **(extra_env or {}),
    }
    result = subprocess.run(
        ["bash", str(SCRIPT), command],
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    return result, args_file.read_text(encoding="utf-8") if args_file.exists() else ""


def run_commit_validation(
    tmp_path: Path,
    *,
    release_sha: str,
    github_sha: str,
    ancestry_exit: int = 0,
) -> tuple[subprocess.CompletedProcess[str], str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    args_file = tmp_path / "git-args"
    fake_git = bin_dir / "git"
    fake_git.write_text(
        "#!/usr/bin/env bash\n"
        'printf "%s\\n" "$*" >> "$GIT_ARGS_FILE"\n'
        'if [[ "$1 $2" == "merge-base --is-ancestor" ]]; then\n'
        '  exit "$ANCESTRY_EXIT"\n'
        "fi\n",
        encoding="utf-8",
    )
    fake_git.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "GIT_ARGS_FILE": str(args_file),
        "ANCESTRY_EXIT": str(ancestry_exit),
        "DEFAULT_BRANCH": "main",
        "RELEASE_SHA": release_sha,
        "GITHUB_SHA": github_sha,
    }
    step = workflow_step("release-preflight.yml", "validate", "Require commit from default branch")
    result = subprocess.run(
        ["bash", "--noprofile", "--norc", "-e", "-o", "pipefail", "-c", step["run"]],
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    return result, args_file.read_text(encoding="utf-8") if args_file.exists() else ""


@pytest.mark.integration
def test_preflight_validates_the_explicit_sha_and_default_branch_ancestry(tmp_path: Path) -> None:
    sha = "a" * 40
    result, args = run_commit_validation(tmp_path, release_sha=sha, github_sha=sha)

    assert result.returncode == 0
    assert "fetch origin main" in args
    assert f"merge-base --is-ancestor {sha} origin/main" in args


@pytest.mark.integration
@pytest.mark.parametrize("release_sha", ["abc", "A" * 40])
def test_preflight_rejects_malformed_release_sha(tmp_path: Path, release_sha: str) -> None:
    result, args = run_commit_validation(tmp_path, release_sha=release_sha, github_sha="a" * 40)

    assert result.returncode == 1
    assert "must be a full lowercase 40-character commit SHA" in result.stdout
    assert args == ""


@pytest.mark.integration
def test_preflight_rejects_selected_ref_sha_mismatch(tmp_path: Path) -> None:
    result, args = run_commit_validation(tmp_path, release_sha="a" * 40, github_sha="b" * 40)

    assert result.returncode == 1
    assert "does not match selected ref commit" in result.stdout
    assert args == ""


@pytest.mark.integration
def test_preflight_rejects_commit_outside_default_branch(tmp_path: Path) -> None:
    sha = "a" * 40
    result, args = run_commit_validation(tmp_path, release_sha=sha, github_sha=sha, ancestry_exit=1)

    assert result.returncode == 1
    assert "is not reachable from main" in result.stdout
    assert f"merge-base --is-ancestor {sha} origin/main" in args


@pytest.mark.integration
def test_recent_successful_preflight_authorizes_first_publication(tmp_path: Path) -> None:
    result, args = run_release_script(tmp_path, "authorize", [status_row("release-preflight", "success")])

    assert result.returncode == 0
    assert "Recent release preflight authorizes" in result.stdout
    assert f"commits/{'a' * 40}/status --jq" in args
    assert "--paginate" in args
    assert "per_page=100" in args
    assert "statuses?per_page" not in args


@pytest.mark.integration
def test_combined_status_response_has_no_history_size_limit(tmp_path: Path) -> None:
    rows = [status_row(f"ci/check-{index}", "success") for index in range(150)]
    rows.append(status_row("release-preflight", "success"))

    result, _ = run_release_script(tmp_path, "authorize", rows)

    assert result.returncode == 0
    assert "Recent release preflight authorizes" in result.stdout


@pytest.mark.integration
def test_prior_deployment_authorizes_redeployment(tmp_path: Path) -> None:
    old = datetime.now(UTC) - timedelta(days=30)
    result, _ = run_release_script(
        tmp_path,
        "authorize",
        [status_row("deploy/1.2.3", "success", old), status_row("release-preflight", "failure")],
    )

    assert result.returncode == 0
    assert "Prior successful deployment authorizes" in result.stdout


@pytest.mark.integration
@pytest.mark.parametrize("state_value", ["pending", "failure", "error"])
def test_non_successful_preflight_rejects_publication(tmp_path: Path, state_value: str) -> None:
    result, _ = run_release_script(tmp_path, "authorize", [status_row("release-preflight", state_value)])

    assert result.returncode == 1
    assert f"status is {state_value}, not success" in result.stdout


@pytest.mark.integration
def test_stale_preflight_rejects_first_publication(tmp_path: Path) -> None:
    stale = datetime.now(UTC) - timedelta(hours=25)
    result, _ = run_release_script(tmp_path, "authorize", [status_row("release-preflight", "success", stale)])

    assert result.returncode == 1
    assert "outside the 24-hour publication window" in result.stdout


@pytest.mark.integration
def test_small_future_clock_skew_is_allowed(tmp_path: Path) -> None:
    future = datetime.now(UTC) + timedelta(minutes=4)
    result, _ = run_release_script(tmp_path, "authorize", [status_row("release-preflight", "success", future)])

    assert result.returncode == 0
    assert "Recent release preflight authorizes" in result.stdout


@pytest.mark.integration
def test_excessive_future_clock_skew_has_distinct_error(tmp_path: Path) -> None:
    future = datetime.now(UTC) + timedelta(minutes=6)
    result, _ = run_release_script(tmp_path, "authorize", [status_row("release-preflight", "success", future)])

    assert result.returncode == 1
    assert "exceeds the allowed five-minute future clock skew" in result.stdout
    assert "24-hour publication window" not in result.stdout


@pytest.mark.integration
def test_deployment_status_for_another_tag_does_not_authorize(tmp_path: Path) -> None:
    result, _ = run_release_script(tmp_path, "authorize", [status_row("deploy/2.0.0", "success")])

    assert result.returncode == 1
    assert "No release preflight status exists" in result.stdout


@pytest.mark.integration
def test_pending_command_records_preflight_context(tmp_path: Path) -> None:
    result, args = run_release_script(tmp_path, "pending")

    assert result.returncode == 0
    assert "state=pending" in args
    assert "context=release-preflight" in args


@pytest.mark.integration
def test_preflight_recorder_marks_success_only_after_all_jobs_pass(tmp_path: Path) -> None:
    result, args = run_release_script(
        tmp_path,
        "preflight-result",
        extra_env={"VALIDATE_RESULT": "success", "BUILD_RESULT": "success"},
    )

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
    result, args = run_release_script(
        tmp_path,
        "preflight-result",
        extra_env={"VALIDATE_RESULT": validate_result, "BUILD_RESULT": build_result},
    )

    assert result.returncode == 1
    assert "state=failure" in args
    assert "context=release-preflight" in args


@pytest.mark.integration
def test_deployed_command_records_tag_specific_context(tmp_path: Path) -> None:
    result, args = run_release_script(tmp_path, "deployed", tag="1.2.3")

    assert result.returncode == 0
    assert "state=success" in args
    assert "context=deploy/1.2.3" in args
