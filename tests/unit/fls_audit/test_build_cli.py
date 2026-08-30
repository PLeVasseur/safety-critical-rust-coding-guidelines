import sys
from pathlib import Path

import pytest

from builder import build_cli


@pytest.mark.parametrize(
    ("arguments", "enforce_freshness"),
    [
        ([], False),
        (["--ignore-spec-lock-diff"], False),
        (["--enforce-spec-lock-diff"], True),
    ],
)
def test_local_build_freshness_policy(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    arguments: list[str],
    enforce_freshness: bool,
) -> None:
    calls: list[tuple] = []
    monkeypatch.setattr(sys, "argv", ["make.py", *arguments])
    monkeypatch.setattr(build_cli, "build_docs", lambda *values: calls.append(values))

    build_cli.main(tmp_path)

    assert len(calls) == 1
    assert calls[0][6] is enforce_freshness
