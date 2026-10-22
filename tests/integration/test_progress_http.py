"""Real loopback server and CLI lifecycle, with a deterministic offline model stub."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from datetime import UTC, datetime

import httpx
import pytest

from tests.unit.test_progress_report import FIXTURE, report_for


@pytest.mark.parametrize("fixed_port", [False, True])
def test_report_command_http_sse_export_and_shutdown(tmp_path, unused_tcp_port, fixed_port):
    port = unused_tcp_port if fixed_port else 0
    source = tmp_path / "rollout.jsonl"
    records = [json.loads(line) for line in FIXTURE.read_text().splitlines()]
    records[0]["payload"]["timestamp"] = datetime.now(UTC).isoformat()
    records[0]["payload"].update(source="cli", thread_source="user")
    source.write_text("".join(json.dumps(record) + "\n" for record in records))
    folder = tmp_path / "2026/10/22"
    folder.mkdir(parents=True)
    second = folder / "rollout-second.jsonl"
    records[0]["payload"]["id"] = "second-session"
    records[0]["payload"]["cwd"] = "/home/user/search-demo"
    second.write_text("".join(json.dumps(record) + "\n" for record in records))
    config = tmp_path / "config.toml"
    state = tmp_path / "state"
    config.write_text(f'sessions_root="{tmp_path}"\nstate_dir="{state}"\n')
    output = tmp_path / "report.html"
    bindir = tmp_path / "bin"
    bindir.mkdir()
    stub = bindir / "codex"
    template = report_for("placeholder").model_dump()
    stub.write_text(
        f"#!{sys.executable}\n"
        "import json,sys,pathlib\n"
        'if "--version" in sys.argv:\n'
        ' print("codex-cli 0.163.0");sys.exit(0)\n'
        'packet=json.loads(sys.stdin.read().split("OBSERVATION DATA:\\n",1)[1])\n'
        f"report=json.loads({json.dumps(template)!r})\n"
        'ref=packet["events"][-1]["id"]\n'
        'for c in [report["summary"],*report["done"],*report["remaining"],'
        'report["current_action"],*report["tasks"]]: c["refs"]=[ref]\n'
        'pathlib.Path(sys.argv[sys.argv.index("-o")+1]).write_text(json.dumps(report))\n'
    )
    stub.chmod(0o700)
    env = {**os.environ, "PATH": str(bindir) + os.pathsep + os.environ["PATH"]}
    with (tmp_path / "stdout").open("w+") as stdout, (tmp_path / "stderr").open("w+") as stderr:
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "codex_watchtower.cli",
                "report",
                "--config",
                str(config),
                "--log",
                str(source),
                "--output",
                str(output),
                "--port",
                str(port),
            ],
            stdout=stdout,
            stderr=stderr,
            env=env,
        )
        try:
            url = None
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                stdout.seek(0)
                lines = stdout.read().splitlines()
                url = next(
                    (
                        line.removeprefix("Report: ")
                        for line in lines
                        if line.startswith("Report: ")
                    ),
                    None,
                )
                if url:
                    break
                assert process.poll() is None
                time.sleep(0.05)
            assert url and url.startswith("http://127.0.0.1:")
            if fixed_port:
                assert url == f"http://127.0.0.1:{port}/"
            with httpx.Client(base_url=url, timeout=15, trust_env=False) as client:
                deadline = time.monotonic() + 15
                snapshot = client.get("/report.json").json()
                while snapshot["version"] == 0 and time.monotonic() < deadline:
                    time.sleep(0.05)
                    snapshot = client.get("/report.json").json()
                assert snapshot["version"] == 1, snapshot
                page = client.get("/")
                assert page.status_code == 200 and "EventSource" in page.text
                assert "__INITIAL_SNAPSHOT__" not in output.read_text()
                for _ in range(2):
                    with client.stream(
                        "GET", "/events", headers={"Last-Event-ID": "old"}
                    ) as stream:
                        for line in stream.iter_lines():
                            if line.startswith("data: "):
                                assert json.loads(line[6:])["version"] == 1
                                break
                assert client.get("/rollout.jsonl").status_code == 404
                rows = client.get("/sessions", params={"q": "search-demo"}).json()
                assert len(rows) == 1 and rows[0]["session_id"] == "second-session"
                with client.stream("GET", "/events") as stream:
                    lines = stream.iter_lines()
                    for line in lines:
                        if line.startswith("data: "):
                            assert json.loads(line[6:])["session_id"] == snapshot["session_id"]
                            break
                    selected = client.post("/session", json={"key": rows[0]["key"]}).json()
                    assert selected["session_id"] == "second-session"
                    assert selected["selection_revision"] == 1
                    for line in lines:
                        if line.startswith("data: "):
                            update = json.loads(line[6:])
                            if update["session_id"] == "second-session":
                                assert update["selection_revision"] == 1
                                break
                original = next(
                    row
                    for row in client.get("/sessions").json()
                    if row["session_id"] == snapshot["session_id"]
                )
                restored = client.post("/session", json={"key": original["key"]}).json()
                assert restored["version"] == 1 and restored["selection_revision"] == 2
                # A browser keeps SSE open; it must not prevent Ctrl+C shutdown.
                with client.stream("GET", "/events") as stream:
                    assert stream.status_code == 200
                    process.send_signal(signal.SIGINT)
                    process.wait(timeout=10)
        finally:
            if process.poll() is None:
                process.send_signal(signal.SIGINT)
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        assert process.returncode == 0
        assert output.exists()
