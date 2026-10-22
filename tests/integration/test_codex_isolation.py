"""Opt-in probe of the installed CLI; sends synthetic input only to a local mock."""

from __future__ import annotations

import json
import os
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from codex_watchtower.models.codex_cli import VERIFIED_CLI_VERSION, build_command
from codex_watchtower.report.contract import ProgressReport


@pytest.mark.skipif(
    os.environ.get("WATCHTOWER_PROBE_CODEX") != "1",
    reason="requires installed Codex and permission to bind a loopback socket",
)
def test_installed_cli_sends_no_tools(tmp_path):
    captured = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"models":[]}')

        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            captured.append(json.loads(body))
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b'{"error":{"message":"isolation probe complete"}}')

        def log_message(self, *_):
            pass

    version = subprocess.check_output(["codex", "--version"], text=True).strip()
    assert version == VERIFIED_CLI_VERSION
    (tmp_path / "schema.json").write_text(json.dumps(ProgressReport.model_json_schema()))
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        command = build_command(tmp_path, "gpt-6-luna")
        overrides = [
            'model_provider="probe"',
            'model_providers.probe.name="probe"',
            f'model_providers.probe.base_url="http://127.0.0.1:{server.server_port}/v1"',
            'model_providers.probe.wire_api="responses"',
        ]
        command = command[:-1]
        for value in overrides:
            command.extend(["-c", value])
        result = subprocess.run(
            command + ["-"],
            input="Synthetic probe; reply with JSON.",
            text=True,
            capture_output=True,
            timeout=45,
        )
        assert captured, result.stderr
        assert all(not body.get("tools") for body in captured)
        assert "unrecognized configuration" not in result.stdout
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
