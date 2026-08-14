from __future__ import annotations

import subprocess
import sys
import time

from codex_watchtower.launcher.adopt import (
    AdoptionOutcome,
    CandidateProcess,
    check_adopted_process,
    find_adoption_candidate,
    is_pid_alive,
)


def test_unique_match_by_workspace_and_start_order() -> None:
    candidates = [CandidateProcess(pid=111, cwd="/w/project", start_time=1000.0)]
    result = find_adoption_candidate(candidates, workspace="/w/project", first_record_time=1005.0)
    assert result.pid == 111
    assert result.method == "adopted"


def test_zero_candidates_yields_no_match() -> None:
    result = find_adoption_candidate([], workspace="/w/project", first_record_time=1005.0)
    assert result.pid is None
    assert result.method == "none"


def test_wrong_workspace_is_not_a_candidate() -> None:
    candidates = [CandidateProcess(pid=111, cwd="/w/other", start_time=1000.0)]
    result = find_adoption_candidate(candidates, workspace="/w/project", first_record_time=1005.0)
    assert result.method == "none"


def test_multiple_matching_candidates_yields_no_match() -> None:
    candidates = [
        CandidateProcess(pid=111, cwd="/w/project", start_time=1000.0),
        CandidateProcess(pid=222, cwd="/w/project", start_time=1001.0),
    ]
    result = find_adoption_candidate(candidates, workspace="/w/project", first_record_time=1005.0)
    assert result.method == "none"
    assert result.pid is None


def test_pid_reuse_rejected_by_start_time_after_first_record() -> None:
    """A process that started *after* the rollout's first record cannot be its author."""
    candidates = [CandidateProcess(pid=111, cwd="/w/project", start_time=2000.0)]
    result = find_adoption_candidate(candidates, workspace="/w/project", first_record_time=1005.0)
    assert result.method == "none"
    assert result.pid is None


# --- liveness and disappearance ------------------------------------------


def test_is_pid_alive_true_for_running_process() -> None:
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(5)"])
    try:
        assert is_pid_alive(proc.pid) is True
    finally:
        proc.terminate()
        proc.wait(timeout=5)


def test_is_pid_alive_false_after_process_exits() -> None:
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait(timeout=5)
    # Give the OS a brief moment; a reaped child's pid is no longer live.
    for _ in range(20):
        if not is_pid_alive(proc.pid):
            break
        time.sleep(0.05)
    assert is_pid_alive(proc.pid) is False


def test_check_adopted_process_reports_disappearance() -> None:
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait(timeout=5)
    for _ in range(20):
        outcome = check_adopted_process(proc.pid)
        if outcome.disappeared:
            break
        time.sleep(0.05)
    assert outcome.disappeared is True


def test_adoption_outcome_carries_no_exit_code() -> None:
    """Structural guard for spec 5.11: disappearance without a recorded exit

    code must yield idle, never terminal_failed. AdoptionOutcome has no
    exit_code field at all, so a caller has nothing it could pass to a
    terminal-failure transition -- the type itself makes that path
    unrepresentable rather than relying on callers to remember not to.
    """
    assert not hasattr(AdoptionOutcome, "exit_code")
    assert set(AdoptionOutcome.__dataclass_fields__.keys()) == {"disappeared"}
