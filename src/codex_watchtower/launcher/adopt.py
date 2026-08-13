"""Process adoption for sessions started outside `watchtower run` (spec 5.11).

Adoption is inference, not identity: it matches a live process whose
working directory equals the session workspace and whose start time
precedes the first rollout record, then watches for that PID to disappear.
Disappearance without a recorded exit code yields ``idle``, never
``terminal_failed``, because the exit status is unknown. It requires a
unique match and is off by default (an operator opt-in), consistent with
every other correlation path in this module: ambiguity returns no match
rather than guessing.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class CandidateProcess:
    pid: int
    cwd: str
    start_time: float  # process start time, comparable to the rollout's first-record time


@dataclass(frozen=True, slots=True)
class AdoptionResult:
    pid: int | None
    method: str  # "adopted" | "none"


def find_adoption_candidate(
    candidates: list[CandidateProcess], *, workspace: str, first_record_time: float
) -> AdoptionResult:
    """Select a unique live process matching workspace and start-time ordering.

    ``first_record_time`` is the first rollout record's timestamp (as a
    POSIX timestamp); a candidate qualifies only if its process start
    preceded that record, since the process must have existed before it
    could have written to the rollout.
    """
    matches = [c for c in candidates if c.cwd == workspace and c.start_time <= first_record_time]
    unique_pids = {c.pid for c in matches}
    if len(unique_pids) == 1:
        return AdoptionResult(pid=next(iter(unique_pids)), method="adopted")
    return AdoptionResult(pid=None, method="none")


def is_pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, but owned by someone else
    return True


@dataclass(frozen=True, slots=True)
class AdoptionOutcome:
    disappeared: bool


def check_adopted_process(pid: int) -> AdoptionOutcome:
    """Poll an adopted PID. Disappearance carries no exit code, so callers must map it to idle."""
    return AdoptionOutcome(disappeared=not is_pid_alive(pid))


def process_start_time(pid: int) -> float | None:
    """Best-effort process start time (seconds since epoch) via /proc, Linux only.

    Returns None when unavailable (non-Linux, permission denied, or the
    process has already exited), which callers must treat as "cannot use
    this candidate" rather than a match.
    """
    stat_path = Path(f"/proc/{pid}/stat")
    try:
        content = stat_path.read_text()
    except OSError:
        return None
    # Fields are space-separated after the ")" that closes the (comm) field,
    # which may itself contain spaces/parens.
    try:
        after_comm = content.rsplit(")", 1)[1].split()
        clock_ticks_since_boot = float(after_comm[19])  # field 22 overall: starttime
        clock_tick = os.sysconf("SC_CLK_TCK")
        with Path("/proc/stat").open() as f:
            btime = next(int(line.split()[1]) for line in f if line.startswith("btime"))
        return btime + clock_ticks_since_boot / clock_tick
    except (IndexError, ValueError, StopIteration, OSError):
        return None
