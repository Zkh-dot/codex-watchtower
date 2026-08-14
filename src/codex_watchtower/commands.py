"""CLI subcommand dispatch, kept out of cli.py so ``--version`` stays import-light."""

from __future__ import annotations

import argparse

from codex_watchtower import service

_HANDLERS = {
    "serve": service.serve_main,
    "run": service.run_main,
    "inspect": service.inspect_main,
    "assess": service.assess_main,
    "doctor": service.doctor_main,
}


def dispatch(command: str, args: argparse.Namespace) -> int:
    handler = _HANDLERS.get(command)
    if handler is None:
        print(f"Unknown command: {command}")
        return 2
    return handler(args)
