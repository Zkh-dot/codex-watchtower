"""Incremental, restart-safe, complete-line-only rollout reader (spec 5.2).

Identity design, restated briefly (see the spec for the full argument):

- The **source locator** (device, inode, byte offset, record length) is
  provenance only. It is exactly what changes across rotation, copy-truncate,
  and device migration, so it cannot carry identity.
- The **logical event id** is content-addressed: ``hash(session_id,
  record_ordinal, kind, payload_hash)``. ``record_ordinal`` is the record's
  absolute zero-based position from the start of the session file, valid
  only under an append-only precondition.
- This module owns verifying that precondition on every resume. It does so
  with a single mechanism: hash the bytes ``[0, checkpoint.byte_offset)`` of
  whatever file now sits at the tracked path and compare that hash to the
  checkpoint's stored ``checkpoint_hash``.
    - **Match** -> the verified prefix is intact regardless of whether
      device/inode changed underneath it (rotation, remount, hardlink swap).
      Resume tailing at ``byte_offset``, continuing ``record_ordinal``. This
      is the "identity mismatch that preserves the verified prefix" case.
    - **Mismatch, including a file now shorter than the checkpoint offset**
      (truncation) -> the append-only precondition is broken. This is the
      "prefix-hash mismatch" case: fail closed, caller marks the session
      ``identity_broken``, and this module makes no further attempt to
      re-identify events for that file.

Because the check is content-hash-based rather than size- or
device/inode-based, it is uniformly correct across inode replacement, inode
reuse, copy-truncate, "fast regrowth" past the old offset, and device
migration: any of those either preserve the exact prefix bytes (safe to
continue) or they do not (fail closed). No special-casing per failure mode
is needed or present.

The cursor's ``byte_offset`` only ever advances past *complete* lines, so a
trailing partial final line is simply left unread on disk at that offset;
the next call re-reads it naturally along with whatever was appended after
it. No separate in-memory partial buffer needs to be threaded between calls.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import stat as stat_module
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from codex_watchtower.storage.repository import CursorState, SourceLocator

EMPTY_PREFIX_HASH = hashlib.sha256(b"").hexdigest()

MAX_LINE_BYTES = 8 * 1024 * 1024  # a single record longer than this is quarantined


class TailerSafetyError(RuntimeError):
    """A candidate rollout file failed an ownership/type/path safety check."""


@dataclass(frozen=True, slots=True)
class RawRecord:
    record_ordinal: int
    raw_line: bytes  # without trailing newline
    parsed: dict[str, Any] | None  # None => newline-terminated but not valid JSON
    locator: SourceLocator


@dataclass(frozen=True, slots=True)
class IdentityBroken:
    detail: str


@dataclass(frozen=True, slots=True)
class ReadResult:
    records: list[RawRecord]
    cursor: CursorState
    trailing_partial: bytes
    malformed_count: int


def _hash_prefix_bytes(path: Path, length: int) -> bytes | None:
    """Read+hash the first ``length`` bytes of ``path``; None if it has fewer."""
    hasher = hashlib.sha256()
    remaining = length
    read_total = 0
    with path.open("rb") as f:
        while remaining > 0:
            chunk = f.read(min(remaining, 1024 * 1024))
            if not chunk:
                break
            hasher.update(chunk)
            read_total += len(chunk)
            remaining -= len(chunk)
    if read_total < length:
        return None
    return hasher.digest()


def resolve_start(path: Path, stored_cursor: CursorState | None) -> CursorState | IdentityBroken:
    """Determine where tailing should resume for ``path``.

    Re-hashes the verified prefix from disk rather than trusting any
    carried-over in-memory state, so this function alone is the complete
    recovery decision -- callers never need process-lifetime state to call
    it correctly after a restart.
    """
    st = path.stat()
    if stored_cursor is None or stored_cursor.byte_offset == 0:
        # No prior cursor, or an empty verified prefix, which trivially
        # matches any file.
        return CursorState(
            device=st.st_dev,
            inode=st.st_ino,
            byte_offset=0,
            record_ordinal=0,
            checkpoint_hash=EMPTY_PREFIX_HASH,
        )

    digest = _hash_prefix_bytes(path, stored_cursor.byte_offset)
    if digest is None:
        return IdentityBroken(
            detail=(
                f"{path}: file is shorter ({st.st_size} bytes) than the checkpoint "
                f"offset ({stored_cursor.byte_offset}); truncation violates the "
                "append-only precondition"
            )
        )
    if digest.hex() != stored_cursor.checkpoint_hash:
        return IdentityBroken(
            detail=(
                f"{path}: prefix hash mismatch at offset {stored_cursor.byte_offset}; "
                "content before the checkpoint changed, violating the append-only "
                "precondition"
            )
        )
    return replace(stored_cursor, device=st.st_dev, inode=st.st_ino)


def open_safe(
    path: Path,
    sessions_root: Path,
    *,
    expected_uid: int | None = None,
    max_file_size: int = 512 * 1024 * 1024,
) -> int:
    """Open ``path`` for reading, enforcing spec 5.2's file-safety requirements.

    Returns a raw file descriptor (caller is responsible for closing it) so
    the O_NOFOLLOW open and the post-open identity re-check happen on the
    exact same descriptor with no window for a swap in between.
    """
    resolved_root = sessions_root.resolve()
    try:
        resolved_path = path.resolve(strict=True)
    except OSError as exc:
        raise TailerSafetyError(f"{path}: cannot resolve path: {exc}") from exc
    if not resolved_path.is_relative_to(resolved_root):
        raise TailerSafetyError(f"{path}: resolves outside sessions root {sessions_root}")

    pre_stat = os.lstat(path)
    if stat_module.S_ISLNK(pre_stat.st_mode):
        raise TailerSafetyError(f"{path}: symlinks are not opened directly")

    # O_NONBLOCK matters only for a FIFO: without it, opening one for
    # read-only blocks until a writer connects, which would hang ingestion
    # on exactly the kind of non-regular file this check exists to reject.
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | os.O_NONBLOCK
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise TailerSafetyError(f"{path}: symlink rejected by O_NOFOLLOW") from exc
        raise TailerSafetyError(f"{path}: cannot open: {exc}") from exc

    try:
        post_stat = os.fstat(fd)
        if not stat_module.S_ISREG(post_stat.st_mode):
            raise TailerSafetyError(f"{path}: not a regular file (mode={oct(post_stat.st_mode)})")
        if (post_stat.st_dev, post_stat.st_ino) != (pre_stat.st_dev, pre_stat.st_ino):
            raise TailerSafetyError(f"{path}: identity changed between check and open")
        if expected_uid is not None and post_stat.st_uid != expected_uid:
            raise TailerSafetyError(
                f"{path}: owned by uid {post_stat.st_uid}, expected {expected_uid}"
            )
        if post_stat.st_size > max_file_size:
            raise TailerSafetyError(f"{path}: {post_stat.st_size} bytes exceeds configured limit")
        # Clear O_NONBLOCK now that we know this is a regular file; callers
        # read it with ordinary blocking semantics from here on.
        os.set_blocking(fd, True)
    except TailerSafetyError:
        os.close(fd)
        raise
    return fd


def read_new_records(
    path: Path,
    cursor: CursorState,
    *,
    sessions_root: Path | None = None,
    expected_uid: int | None = None,
) -> ReadResult:
    """Read every complete newline-terminated record appended since ``cursor``.

    A trailing partial (final) line is never parsed and never advances the
    cursor past it; it is simply left on disk for the next call to re-read
    alongside whatever gets appended after it.

    Safety: when ``sessions_root`` is provided, the file is opened with
    ``O_NOFOLLOW`` and identity-checked on the same fd. The prefix hash
    and new content are read through the same fd to eliminate the
    TOCTOU window between ``resolve_start`` and content reads.
    """
    if sessions_root is not None:
        fd = open_safe(path, sessions_root, expected_uid=expected_uid)
        file_obj = os.fdopen(fd, "rb")
    else:
        file_obj = path.open("rb")

    with file_obj as f:
        # Read and hash the prefix on the same fd — no re-open.
        hasher = hashlib.sha256()
        remaining_prefix = cursor.byte_offset
        while remaining_prefix > 0:
            chunk = f.read(min(remaining_prefix, 1024 * 1024))
            if not chunk:
                break
            hasher.update(chunk)
            remaining_prefix -= len(chunk)

        # Read new bytes from the same fd.
        new_bytes = f.read()

    lines = new_bytes.split(b"\n")
    complete_lines = lines[:-1]
    trailing_partial = lines[-1]

    records: list[RawRecord] = []
    offset = cursor.byte_offset
    ordinal = cursor.record_ordinal
    malformed = 0

    for raw_line in complete_lines:
        line_len = len(raw_line) + 1  # + newline
        if len(raw_line) > MAX_LINE_BYTES:
            malformed += 1
        else:
            try:
                parsed_any: Any = json.loads(raw_line)
                parsed: dict[str, Any] | None = parsed_any if isinstance(parsed_any, dict) else None
            except json.JSONDecodeError:
                parsed = None
            if parsed is None:
                malformed += 1
            locator = SourceLocator(
                device=cursor.device,
                inode=cursor.inode,
                byte_offset=offset,
                record_length=line_len,
            )
            records.append(
                RawRecord(record_ordinal=ordinal, raw_line=raw_line, parsed=parsed, locator=locator)
            )
        hasher.update(raw_line + b"\n")
        offset += line_len
        ordinal += 1

    new_cursor = CursorState(
        device=cursor.device,
        inode=cursor.inode,
        byte_offset=offset,
        record_ordinal=ordinal,
        checkpoint_hash=hasher.hexdigest(),
    )
    return ReadResult(
        records=records,
        cursor=new_cursor,
        trailing_partial=trailing_partial,
        malformed_count=malformed,
    )
