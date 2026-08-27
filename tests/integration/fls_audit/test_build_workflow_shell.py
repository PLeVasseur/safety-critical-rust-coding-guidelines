import os
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]


def build_script() -> str:
    workflow = yaml.load(
        (ROOT / ".github" / "workflows" / "build-guidelines.yml").read_text(encoding="utf-8"),
        Loader=yaml.BaseLoader,
    )
    step = next(step for step in workflow["jobs"]["build"]["steps"] if step.get("name") == "Build documentation")
    return step["run"]


def run_build_script(
    tmp_path: Path,
    *,
    enforce: bool,
    uv_exit: int = 0,
    uv_output: str = "build complete",
) -> tuple[subprocess.CompletedProcess[str], str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    args_file = tmp_path / "uv-args"
    stub = bin_dir / "uv"
    stub.write_text(
        """#!/usr/bin/env bash
printf '%s\n' "$*" > "$UV_ARGS_FILE"
printf '%s\n' "$UV_OUTPUT"
exit "$UV_EXIT"
""",
        encoding="utf-8",
    )
    stub.chmod(0o755)
    env = {
        **os.environ,
        "ENFORCE_SPEC_LOCK": "true" if enforce else "false",
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "UV_ARGS_FILE": str(args_file),
        "UV_EXIT": str(uv_exit),
        "UV_OUTPUT": uv_output,
    }
    result = subprocess.run(
        ["bash", "--noprofile", "--norc", "-e", "-o", "pipefail", "-c", build_script()],
        cwd=tmp_path,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    return result, args_file.read_text(encoding="utf-8")


@pytest.mark.integration
@pytest.mark.parametrize(("enforce", "ignored"), [(False, True), (True, False)])
def test_build_shell_selects_freshness_policy(tmp_path: Path, enforce: bool, ignored: bool) -> None:
    result, args = run_build_script(tmp_path, enforce=enforce)

    assert result.returncode == 0, result.stdout + result.stderr
    assert ("--ignore-spec-lock-diff" in args) is ignored


@pytest.mark.integration
def test_build_shell_preserves_make_exit_status(tmp_path: Path) -> None:
    result, _ = run_build_script(tmp_path, enforce=False, uv_exit=17, uv_output="ordinary build failure")

    assert result.returncode == 17


@pytest.mark.integration
def test_build_shell_fails_when_successful_command_prints_traceback(tmp_path: Path) -> None:
    result, _ = run_build_script(tmp_path, enforce=False, uv_output="Traceback (most recent call last)")

    assert result.returncode == 1
    assert "Build errors detected in log" in result.stdout


@pytest.mark.integration
def test_build_shell_copies_sphinx_traceback(tmp_path: Path) -> None:
    source = Path("/tmp") / f"sphinx-err-{tmp_path.parent.name}-{tmp_path.name}.log"
    source.write_text("sphinx traceback\n", encoding="utf-8")
    try:
        result, _ = run_build_script(
            tmp_path,
            enforce=False,
            uv_output=f"Traceback (most recent call last)\n{source}",
        )
    finally:
        source.unlink(missing_ok=True)

    assert result.returncode == 1
    assert (tmp_path / "build" / "sphinx_traceback.log").read_text(encoding="utf-8") == "sphinx traceback\n"


@pytest.mark.integration
@pytest.mark.parametrize("uv_exit", [0, 17])
def test_build_shell_copies_fls_difference_without_masking_status(tmp_path: Path, uv_exit: int) -> None:
    source = Path("/tmp") / f"fls_diff_{tmp_path.parent.name}-{tmp_path.name}.txt"
    source.write_text("spec lock difference\n", encoding="utf-8")
    try:
        result, _ = run_build_script(tmp_path, enforce=False, uv_exit=uv_exit, uv_output=str(source))
    finally:
        source.unlink(missing_ok=True)

    assert result.returncode == uv_exit
    assert (tmp_path / "build" / "spec_lock_file_differences.txt").read_text(encoding="utf-8") == (
        "spec lock difference\n"
    )


@pytest.mark.integration
def test_build_shell_missing_diagnostic_does_not_mask_status(tmp_path: Path) -> None:
    missing = Path("/tmp") / f"fls_diff_missing-{tmp_path.parent.name}-{tmp_path.name}.txt"

    result, _ = run_build_script(tmp_path, enforce=False, uv_exit=17, uv_output=str(missing))

    assert result.returncode == 17
    assert not (tmp_path / "build" / "spec_lock_file_differences.txt").exists()
