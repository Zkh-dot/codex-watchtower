"""Tool-free Codex subprocess adapter for structured progress reports."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import tempfile
from pathlib import Path

from codex_watchtower.report.contract import ProgressReport

# Verified by capturing the installed CLI's actual Responses request in a local probe.
DISABLED_FEATURES = (
    "shell_tool",
    "apply_patch_freeform",
    "multi_agent",
    "apps",
    "browser_use",
    "remote_plugin",
    "view_image",
    "sleep_tool",
    "hooks",
    "memories",
    "goals",
    "code_mode",
    "computer_use",
    "skill_search",
    "tool_suggest",
)
VERIFIED_CLI_VERSION = "codex-cli 0.163.0"


def output_schema(packet: str) -> dict[str, object]:
    data = json.loads(packet)
    refs = sorted({e["id"] for e in [*data["events"], *data["previous_evidence"]]})
    if not refs:
        raise ValueError("analysis requires at least one evidence event")
    schema = ProgressReport.model_json_schema()
    for name in ("Claim", "Task"):
        schema["$defs"][name]["properties"]["refs"]["items"]["enum"] = refs
    return schema


def build_command(workdir: Path, model: str) -> list[str]:
    command = [
        "codex",
        "exec",
        "--ignore-user-config",
        "--ignore-rules",
        "--ephemeral",
        "--skip-git-repo-check",
        "--sandbox",
        "read-only",
        "--json",
        "--color",
        "never",
        "-C",
        str(workdir),
        "-m",
        model,
        "--output-schema",
        str(workdir / "schema.json"),
        "-o",
        str(workdir / "result.json"),
    ]
    for feature in DISABLED_FEATURES:
        command.extend(["--disable", feature])
    for setting in (
        'web_search="disabled"',
        'model_reasoning_effort="medium"',
        "project_doc_max_bytes=0",
        "features.skip_host_skill_discovery=true",
        "features.enable_request_compression=false",
    ):
        command.extend(["-c", setting])
    return command + ["-"]


async def run_codex(packet: str, model: str, *, timeout: float = 180) -> ProgressReport:
    version = await asyncio.create_subprocess_exec(
        "codex", "--version", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL
    )
    try:
        stdout_version, _ = await asyncio.wait_for(version.communicate(), timeout=10)
    except (TimeoutError, asyncio.CancelledError):
        version.kill()
        await version.wait()
        raise
    # shortcut: tool isolation was verified for this CLI release, probe before upgrading.
    if version.returncode or stdout_version.decode().strip() != VERIFIED_CLI_VERSION:
        raise RuntimeError("Unverified Codex CLI version; verify tool isolation before upgrading")
    with tempfile.TemporaryDirectory(prefix="watchtower-assess-") as directory:
        workdir = Path(directory)
        (workdir / "schema.json").write_text(json.dumps(output_schema(packet)), encoding="utf-8")
        prompt = Path(__file__).with_name("prompts").joinpath("progress.txt").read_text()
        env = {
            k: v
            for k, v in os.environ.items()
            if k not in {"CODEX_THREAD_ID", "CODEX_SESSION_ID", "CODEX_TOOL_CALL_ID", "CODEX_CI"}
        }
        with (
            (workdir / "events.jsonl").open("wb") as stdout,
            (workdir / "stderr.log").open("wb") as stderr,
        ):
            process = await asyncio.create_subprocess_exec(
                *build_command(workdir, model),
                stdin=asyncio.subprocess.PIPE,
                stdout=stdout,
                stderr=stderr,
                env=env,
                start_new_session=True,
            )
            try:
                await asyncio.wait_for(
                    process.communicate((prompt + "\n\nOBSERVATION DATA:\n" + packet).encode()),
                    timeout=timeout,
                )
            except (TimeoutError, asyncio.CancelledError):
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                await process.wait()
                raise
        if process.returncode:
            # Never include raw provider stderr, which may contain credentials or transcripts.
            raise RuntimeError(
                f"Codex analysis failed (exit {process.returncode}); check CLI login"
            )
        result = workdir / "result.json"
        if not result.exists() or result.stat().st_size > 128_000:
            raise ValueError("Codex response is missing or exceeds 128 KB")
        return ProgressReport.model_validate_json(result.read_text(encoding="utf-8"))
