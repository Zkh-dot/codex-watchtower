from __future__ import annotations

from pathlib import Path

from codex_watchtower.cli import build_parser, main
from codex_watchtower.service import DoctorCheck, resolve_config_path, run_doctor_checks

REPO_ROOT = Path(__file__).resolve().parents[2]


def _write_config(
    tmp_path: Path,
    *,
    sessions_root: Path | None = None,
    agentlens_enabled: bool = False,
    agentlens_base_url: str = "http://127.0.0.1:4316/mcp",
    bad_model: bool = False,
) -> Path:
    state_dir = tmp_path / "state"
    resolved_sessions_root = sessions_root if sessions_root is not None else (tmp_path / "sessions")
    lines = [
        f'sessions_root = "{resolved_sessions_root}"',
        f'state_dir = "{state_dir}"',
        "",
        "[agentlens]",
        f"enabled = {'true' if agentlens_enabled else 'false'}",
        f'base_url = "{agentlens_base_url}"',
    ]
    if bad_model:
        lines += [
            "",
            "[luna]",
            'profile_name = "luna"',
            'endpoint = "http://not-https.example.com"',
            'model_identifier = "x"',
            'trust_mode = "trusted-remote"',
            "remote_consent_given = true",
        ]
    config_path = tmp_path / "config.toml"
    config_path.write_text("\n".join(lines) + "\n")
    return config_path


# --- argument parsing for every subcommand --------------------------------


def test_parser_accepts_all_subcommands() -> None:
    parser = build_parser()
    for argv in (
        ["serve"],
        ["run", "--", "echo", "hi"],
        ["inspect", "sess-1"],
        ["assess", "sess-1"],
        ["doctor"],
    ):
        args = parser.parse_args(argv)
        assert args.command == argv[0]


def test_version_flag_short_circuits_before_any_subcommand_logic(capsys) -> None:  # type: ignore[no-untyped-def]
    code = main(["--version"])
    assert code == 0
    assert "0.1.0" in capsys.readouterr().out


# --- doctor: missing sessions_root -----------------------------------------


def test_doctor_reports_missing_sessions_root(tmp_path: Path) -> None:
    config_path = _write_config(tmp_path, sessions_root=tmp_path / "does-not-exist")
    checks = run_doctor_checks(config_path)
    by_name = {c.name: c for c in checks}
    assert by_name["sessions_root"].healthy is False


def test_doctor_reports_healthy_sessions_root(tmp_path: Path) -> None:
    sessions_root = tmp_path / "sessions"
    sessions_root.mkdir()
    config_path = _write_config(tmp_path, sessions_root=sessions_root)
    checks = run_doctor_checks(config_path)
    by_name = {c.name: c for c in checks}
    assert by_name["sessions_root"].healthy is True


# --- doctor: unavailable AgentLens -----------------------------------------


def test_doctor_reports_unavailable_agentlens(tmp_path: Path) -> None:
    sessions_root = tmp_path / "sessions"
    sessions_root.mkdir()
    # Nothing is listening on this port in the test environment.
    config_path = _write_config(
        tmp_path,
        sessions_root=sessions_root,
        agentlens_enabled=True,
        agentlens_base_url="http://127.0.0.1:1/mcp",
    )
    checks = run_doctor_checks(config_path)
    by_name = {c.name: c for c in checks}
    assert by_name["agentlens"].healthy is False


def test_doctor_reports_agentlens_disabled_as_healthy(tmp_path: Path) -> None:
    sessions_root = tmp_path / "sessions"
    sessions_root.mkdir()
    config_path = _write_config(tmp_path, sessions_root=sessions_root, agentlens_enabled=False)
    checks = run_doctor_checks(config_path)
    by_name = {c.name: c for c in checks}
    assert by_name["agentlens"].healthy is True


# --- doctor: invalid model config -------------------------------------------


def test_doctor_reports_invalid_model_config(tmp_path: Path) -> None:
    config_path = _write_config(tmp_path, bad_model=True)
    checks = run_doctor_checks(config_path)
    assert len(checks) == 1
    assert checks[0].name == "config"
    assert checks[0].healthy is False
    assert "HTTPS" in checks[0].detail


# --- doctor: healthy local setup end to end --------------------------------


def test_doctor_all_checks_healthy(tmp_path: Path) -> None:
    sessions_root = tmp_path / "sessions"
    sessions_root.mkdir()
    config_path = _write_config(tmp_path, sessions_root=sessions_root)
    checks = run_doctor_checks(config_path)
    assert all(c.healthy for c in checks), checks
    assert {c.name for c in checks} == {"config", "sessions_root", "agentlens", "state_dir"}


def test_doctor_missing_config_reports_single_failure(tmp_path: Path) -> None:
    checks = run_doctor_checks(tmp_path / "no-such-config.toml")
    assert len(checks) == 1
    assert checks[0].healthy is False


def test_resolve_config_path_prefers_explicit(tmp_path: Path) -> None:
    explicit = tmp_path / "explicit.toml"
    assert resolve_config_path(str(explicit)) == explicit


# --- doctor_main exit codes -------------------------------------------------


def test_doctor_main_exit_code_reflects_health(tmp_path: Path, capsys, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    sessions_root = tmp_path / "sessions"
    sessions_root.mkdir()
    config_path = _write_config(tmp_path, sessions_root=sessions_root)
    code = main(["doctor", "--config", str(config_path)])
    assert code == 0
    out = capsys.readouterr().out
    assert "[OK] config" in out


def test_doctor_main_nonzero_on_failure(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    config_path = _write_config(tmp_path, sessions_root=tmp_path / "missing")
    code = main(["doctor", "--config", str(config_path)])
    assert code == 1
    out = capsys.readouterr().out
    assert "[FAIL] sessions_root" in out


# --- inspect against a real seeded database ---------------------------------


def test_inspect_prints_reconciled_state(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    import json as jsonlib

    from codex_watchtower.ingest import IngestionService
    from codex_watchtower.storage import db
    from codex_watchtower.storage.repository import Repository

    sessions_root = tmp_path / "sessions"
    rollout = sessions_root / "2026" / "08" / "13" / "rollout-1.jsonl"
    rollout.parent.mkdir(parents=True)
    rollout.write_text(
        jsonlib.dumps(
            {
                "type": "session_meta",
                "timestamp": "2026-08-13T10:00:00Z",
                "payload": {"id": "sess-1", "cwd": "/w"},
            }
        )
        + "\n"
    )
    config_path = _write_config(tmp_path, sessions_root=sessions_root)
    from codex_watchtower.config import load_config

    config = load_config(config_path)
    config.state_dir.mkdir(parents=True, exist_ok=True)
    conn = db.open_database(config.state_dir / "state.db")
    IngestionService(sessions_root, Repository(conn)).poll_once()

    code = main(["inspect", "sess-1", "--config", str(config_path)])
    assert code == 0
    out = capsys.readouterr().out
    assert '"session_id": "sess-1"' in out


def test_inspect_unknown_session_returns_nonzero(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    sessions_root = tmp_path / "sessions"
    sessions_root.mkdir()
    config_path = _write_config(tmp_path, sessions_root=sessions_root)
    code = main(["inspect", "does-not-exist", "--config", str(config_path)])
    assert code == 1


# --- systemd hardening: no root, private temp, restart policy, env file ----


def test_systemd_unit_runs_as_dedicated_unprivileged_user() -> None:
    unit = (REPO_ROOT / "deploy" / "codex-watchtower.service").read_text()
    assert "User=watchtower" in unit
    assert "User=root" not in unit


def test_systemd_unit_has_private_tmp() -> None:
    unit = (REPO_ROOT / "deploy" / "codex-watchtower.service").read_text()
    assert "PrivateTmp=true" in unit


def test_systemd_unit_has_restart_policy() -> None:
    unit = (REPO_ROOT / "deploy" / "codex-watchtower.service").read_text()
    assert "Restart=" in unit


def test_systemd_unit_uses_explicit_environment_file() -> None:
    unit = (REPO_ROOT / "deploy" / "codex-watchtower.service").read_text()
    assert "EnvironmentFile=" in unit


def test_systemd_unit_drops_capabilities() -> None:
    unit = (REPO_ROOT / "deploy" / "codex-watchtower.service").read_text()
    assert "NoNewPrivileges=true" in unit
    assert "CapabilityBoundingSet=" in unit


# --- operations doc documents backup/recovery -------------------------------


def test_operations_doc_documents_backup_and_recovery() -> None:
    doc = (REPO_ROOT / "docs" / "operations.md").read_text().lower()
    assert "backup" in doc
    assert "restore" in doc or "recovery" in doc
    assert "sqlite3" in doc


def test_doctor_check_dataclass_shape() -> None:
    check = DoctorCheck(name="x", healthy=True, detail="ok")
    assert check.name == "x"
    assert check.healthy is True
