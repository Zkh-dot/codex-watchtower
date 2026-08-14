import subprocess
import sys

import codex_watchtower


def test_import() -> None:
    assert codex_watchtower.__version__ == "0.1.0"


def test_version_flag() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "codex_watchtower.cli", "--version"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == "0.1.0"
