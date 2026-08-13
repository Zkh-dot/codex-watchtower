"""Command-line entry point for Codex Watchtower.

Subcommand bodies are wired up in Task 29 once storage/ingest/API/launcher
all exist (see docs/plans/2026-08-13-codex-watchtower-implementation.md).
Until then this module only owns argument parsing and ``--version``.
"""

from __future__ import annotations

import argparse
import sys

from codex_watchtower import __version__


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="watchtower")
    parser.add_argument("--version", action="store_true", help="print the version and exit")
    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser("serve", help="run discovery, ingestion, assessment, API, and notifier")
    run_parser = subparsers.add_parser(
        "run", help="wrap a Codex invocation and capture process exit evidence"
    )
    run_parser.add_argument("command", nargs=argparse.REMAINDER)
    subparsers.add_parser("inspect", help="print current state for a session")
    subparsers.add_parser("assess", help="request an on-demand Terra assessment")
    subparsers.add_parser("doctor", help="check configuration and dependency health")
    return parser


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.version:
        print(__version__)
        return 0

    if args.command is None:
        parser.print_help()
        return 0

    print(f"'{args.command}' is not implemented yet.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
