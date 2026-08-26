import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from exts.coding_guidelines import fls_checks


def app(tmp_path: Path, *, enforce: bool = False) -> SimpleNamespace:
    return SimpleNamespace(
        confdir=tmp_path,
        config=SimpleNamespace(enable_spec_lock_consistency=enforce),
    )


def test_lock_file_is_required_even_when_freshness_is_not_enforced(tmp_path: Path) -> None:
    with pytest.raises(fls_checks.FLSValidationError, match="No FLS lock file"):
        fls_checks.check_fls_lock_consistency(app(tmp_path), object(), {})


def test_lock_file_must_be_valid_json_even_when_freshness_is_not_enforced(tmp_path: Path) -> None:
    (tmp_path / "spec.lock").write_text("not json\n", encoding="utf-8")

    with pytest.raises(fls_checks.FLSValidationError, match="Failed to read FLS lock file"):
        fls_checks.check_fls_lock_consistency(app(tmp_path), object(), {})


@pytest.mark.parametrize("value", [{}, {"documents": []}])
def test_lock_file_must_have_documents_when_freshness_is_not_enforced(tmp_path: Path, value: dict) -> None:
    (tmp_path / "spec.lock").write_text(json.dumps(value), encoding="utf-8")

    with pytest.raises(fls_checks.FLSValidationError, match="documents must be a nonempty list"):
        fls_checks.check_fls_lock_consistency(app(tmp_path), object(), {})


def test_disabled_enforcement_still_computes_freshness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "spec.lock").write_text(json.dumps({"documents": [{"sections": []}]}), encoding="utf-8")
    monkeypatch.setattr(fls_checks, "SphinxNeedsData", lambda _env: SimpleNamespace(get_needs_view=lambda: {}))
    monkeypatch.setattr(fls_checks, "get_tqdm", lambda *, iterable, **_kwargs: iterable)
    monkeypatch.setattr(fls_checks.fls_diff, "extract_paragraphs", lambda value: value)
    monkeypatch.setattr(fls_checks.fls_diff, "diff_paragraphs", lambda _live, _locked: {"changed": True})
    monkeypatch.setattr(fls_checks.fls_diff, "has_differences", lambda _diff: True)
    monkeypatch.setattr(fls_checks.fls_diff, "build_detailed_differences", lambda _diff, _guidelines: (["change"], []))
    monkeypatch.setattr(fls_checks.fls_diff, "write_detailed_report", lambda _details: tmp_path / "details")
    monkeypatch.setattr(fls_checks.fls_diff, "build_summary", lambda _affected, _changed: ["change"])

    assert fls_checks.check_fls_lock_consistency(app(tmp_path), object(), {"live": {}}) == (True, ["change"])


def test_check_fls_ignores_only_detected_drift(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    value = app(tmp_path)
    env = SimpleNamespace(config=SimpleNamespace(offline=False))
    monkeypatch.setattr(fls_checks, "check_fls_exists_and_valid_format", lambda _app, _env: None)
    monkeypatch.setattr(fls_checks, "gather_fls_paragraph_ids", lambda _app, _url: ({}, {"live": {}}))
    monkeypatch.setattr(fls_checks, "check_fls_lock_consistency", lambda _app, _env, _raw: (True, ["change"]))
    monkeypatch.setattr(fls_checks, "check_fls_ids_correct", lambda _app, _env, _ids: None)
    monkeypatch.setattr(fls_checks, "read_fls_ignore_list", lambda _app: [])
    monkeypatch.setattr(fls_checks, "insert_fls_coverage", lambda _app, _env, _ids: None)
    monkeypatch.setattr(fls_checks, "calculate_fls_coverage", lambda _ids, _ignored: {})
    monkeypatch.setattr(fls_checks, "log_coverage_report", lambda _coverage: None)

    fls_checks.check_fls(value, env)

    value.config.enable_spec_lock_consistency = True
    with pytest.raises(fls_checks.FLSValidationError, match="specification has changed"):
        fls_checks.check_fls(value, env)
