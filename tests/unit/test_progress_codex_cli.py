from __future__ import annotations

import json
import os
import sys

import pytest
from pydantic import ValidationError

from codex_watchtower.models.codex_cli import run_codex
from tests.unit.test_progress_report import report_for

PACKET = json.dumps({"events": [{"id": "real"}], "previous_evidence": []})


def stub_cli(tmp_path, monkeypatch, body, version="codex-cli 0.163.0"):
    executable = tmp_path / "codex"
    executable.write_text(
        f"#!{sys.executable}\nimport sys,os,pathlib,time\n"
        f'if "--version" in sys.argv: print({version!r});sys.exit(0)\n' + body
    )
    executable.chmod(0o700)
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ["PATH"])


@pytest.mark.asyncio
async def test_real_subprocess_output_is_parsed(tmp_path, monkeypatch):
    body = report_for("real").model_dump_json()
    stub_cli(
        tmp_path,
        monkeypatch,
        f'pathlib.Path(sys.argv[sys.argv.index("-o")+1]).write_text({body!r})\n',
    )
    report = await run_codex(PACKET, "gpt-6-luna")
    assert report.tasks[0].id == "report"


@pytest.mark.asyncio
async def test_timeout_kills_subprocess(tmp_path, monkeypatch):
    pid = tmp_path / "pid"
    stub_cli(
        tmp_path,
        monkeypatch,
        f"pathlib.Path({str(pid)!r}).write_text(str(os.getpid()))\ntime.sleep(60)\n",
    )
    with pytest.raises(TimeoutError):
        await run_codex(PACKET, "gpt-6-luna", timeout=0.3)
    assert pid.exists()
    with pytest.raises(ProcessLookupError):
        os.kill(int(pid.read_text()), 0)


@pytest.mark.asyncio
async def test_unverified_version_refuses_to_analyze(tmp_path, monkeypatch):
    touched = tmp_path / "called"
    stub_cli(
        tmp_path,
        monkeypatch,
        f"pathlib.Path({str(touched)!r}).touch()\n",
        version="codex-cli 999.0",
    )
    with pytest.raises(RuntimeError, match="Unverified"):
        await run_codex(PACKET, "gpt-6-luna")
    assert not touched.exists()


@pytest.mark.asyncio
async def test_invalid_model_json_and_raw_error_not_exposed(tmp_path, monkeypatch):
    stub_cli(
        tmp_path,
        monkeypatch,
        'pathlib.Path(sys.argv[sys.argv.index("-o")+1]).write_text("not-json")\n',
    )
    with pytest.raises(ValidationError):
        await run_codex(PACKET, "gpt-6-luna")
    stub_cli(tmp_path, monkeypatch, 'sys.stderr.write("sensitive provider text");sys.exit(42)\n')
    with pytest.raises(RuntimeError) as exc:
        await run_codex(PACKET, "gpt-6-luna")
    assert "sensitive" not in str(exc.value)


def test_fast_is_the_actual_service_tier(tmp_path):
    from codex_watchtower.models.codex_cli import build_command

    assert 'service_tier="fast"' in build_command(tmp_path, "gpt-6-luna", fast=True)
    assert not any("service_tier" in item for item in build_command(tmp_path, "gpt-6-luna"))
