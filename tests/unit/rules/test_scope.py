from __future__ import annotations

from pathlib import Path

from codex_watchtower import domain
from codex_watchtower.rules.scope import canonicalize_path, detect_scope_violations


def _file_changed(idx: int, path: str) -> domain.Event:
    return domain.Event(
        id=f"evt:{idx}",
        timestamp="2026-08-13T10:00:00Z",
        kind=domain.EventKind.file_changed,
        summary=f"modified: {path}",
        path=path,
    )


# --- canonicalization ---------------------------------------------------


def test_relative_path_stays_relative(tmp_path: Path) -> None:
    assert canonicalize_path("src/foo.py", tmp_path) == "src/foo.py"


def test_absolute_path_inside_workspace_becomes_relative(tmp_path: Path) -> None:
    absolute = str(tmp_path / "src" / "foo.py")
    assert canonicalize_path(absolute, tmp_path) == "src/foo.py"


def test_dot_dot_segments_are_normalized(tmp_path: Path) -> None:
    assert canonicalize_path("src/../secrets/key.pem", tmp_path) == "secrets/key.pem"


def test_symlink_is_resolved_before_comparison(tmp_path: Path) -> None:
    real_secret_dir = tmp_path / "real_secrets"
    real_secret_dir.mkdir()
    (real_secret_dir / "key.pem").write_text("secret")
    link = tmp_path / "src" / "linked_secrets"
    link.parent.mkdir()
    link.symlink_to(real_secret_dir)
    # Accessed via the symlink, this must still canonicalize to the
    # symlink's *target* location, not to something under src/.
    result = canonicalize_path("src/linked_secrets/key.pem", tmp_path)
    assert result == "real_secrets/key.pem"


def test_path_outside_workspace_is_not_falsely_relative(tmp_path: Path) -> None:
    outside = "/etc/passwd"
    result = canonicalize_path(outside, tmp_path)
    assert result == "/etc/passwd"


# --- forbidden critical, unexpected warning ------------------------------


def test_forbidden_path_change_is_critical(tmp_path: Path) -> None:
    events = [_file_changed(0, "secrets/key.pem")]
    signals = detect_scope_violations(
        events, workspace=tmp_path, expected_paths=[], forbidden_paths=["secrets/"]
    )
    assert len(signals) == 1
    assert signals[0].kind == domain.SignalKind.forbidden_path
    assert signals[0].severity == domain.Severity.critical


def test_unexpected_path_outside_expected_is_warning(tmp_path: Path) -> None:
    events = [_file_changed(0, "other/thing.py")]
    signals = detect_scope_violations(
        events, workspace=tmp_path, expected_paths=["src/"], forbidden_paths=[]
    )
    assert len(signals) == 1
    assert signals[0].kind == domain.SignalKind.scope_expansion
    assert signals[0].severity == domain.Severity.warning


def test_change_within_expected_paths_is_not_flagged(tmp_path: Path) -> None:
    events = [_file_changed(0, "src/foo.py")]
    signals = detect_scope_violations(
        events, workspace=tmp_path, expected_paths=["src/"], forbidden_paths=[]
    )
    assert signals == []


def test_no_expected_paths_configured_means_no_scope_constraint(tmp_path: Path) -> None:
    events = [_file_changed(0, "anywhere/thing.py")]
    signals = detect_scope_violations(
        events, workspace=tmp_path, expected_paths=[], forbidden_paths=[]
    )
    assert signals == []


def test_forbidden_takes_precedence_over_expected(tmp_path: Path) -> None:
    events = [_file_changed(0, "secrets/key.pem")]
    signals = detect_scope_violations(
        events,
        workspace=tmp_path,
        expected_paths=["secrets/"],  # even if "expected", forbidden wins
        forbidden_paths=["secrets/"],
    )
    assert len(signals) == 1
    assert signals[0].kind == domain.SignalKind.forbidden_path
