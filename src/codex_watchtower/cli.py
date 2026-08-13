"""Command-line entry point for Codex Watchtower.

Subcommand bodies live in ``codex_watchtower.commands``/``service``,
imported lazily so ``watchtower --version`` stays cheap and does not pull
in storage/ingest/API/launcher machinery.
"""

from __future__ import annotations

import argparse
import sys

from codex_watchtower import __version__


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="watchtower")
    parser.add_argument("--version", action="store_true", help="print the version and exit")
    subparsers = parser.add_subparsers(dest="command")

    serve_parser = subparsers.add_parser(
        "serve", help="run discovery, ingestion, assessment, API, and notifier"
    )
    serve_parser.add_argument("--config", default=None, help="path to config.toml")

    run_parser = subparsers.add_parser(
        "run", help="wrap a Codex invocation and capture process exit evidence"
    )
    run_parser.add_argument("--config", default=None, help="path to config.toml")
    # dest="argv", not "command": the top-level parser's subparsers already
    # use dest="command" for the subcommand name itself, and REMAINDER here
    # would silently overwrite that if it shared the same attribute.
    run_parser.add_argument("wrapped_argv", nargs=argparse.REMAINDER, metavar="command")

    inspect_parser = subparsers.add_parser("inspect", help="print current state for a session")
    inspect_parser.add_argument("--config", default=None, help="path to config.toml")
    inspect_parser.add_argument("session_id")

    assess_parser = subparsers.add_parser("assess", help="request an on-demand Terra assessment")
    assess_parser.add_argument("--config", default=None, help="path to config.toml")
    assess_parser.add_argument("session_id")

    doctor_parser = subparsers.add_parser(
        "doctor", help="check configuration and dependency health"
    )
    doctor_parser.add_argument("--config", default=None, help="path to config.toml")

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

    from codex_watchtower import commands

    return commands.dispatch(args.command, args)


if __name__ == "__main__":
    raise SystemExit(main())
