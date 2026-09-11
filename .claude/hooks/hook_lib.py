#!/usr/bin/env python3
# Imported by the python hooks in this folder, never executed on its own.
# The JSON each event puts on stdin: https://code.claude.com/docs/en/hooks

import contextlib
import json
import os
import sys

# Locking is platform-split: flock on macOS/Linux, byte-range locking on Windows, which has no fcntl.
try:
    import fcntl

    msvcrt = None
except ModuleNotFoundError:
    import msvcrt

    fcntl = None

FILE_ENCODING = "utf-8"  # transcripts and these logs are UTF-8, never the Windows locale codepage
WINDOWS_LOCK_BYTES = 1  # msvcrt locks a byte range, and one byte is enough to gate the whole file


def read_event():
    # The payload is UTF-8; on Windows sys.stdin would otherwise decode it as the locale codepage.
    sys.stdin.reconfigure(encoding=FILE_ENCODING)
    return json.load(sys.stdin)


def logs_directory(event):
    project_dir = os.environ.get("CLAUDE_PROJECT_DIR") or event.get("cwd")
    return os.path.join(project_dir, ".claude", "logs")


def load_state(state_file):
    try:
        with open(state_file, encoding=FILE_ENCODING) as stored:
            state = json.load(stored)
    except (OSError, ValueError):
        return {}
    return state if isinstance(state, dict) else {}


def save_state(state_file, state):
    with open(state_file, "w", encoding=FILE_ENCODING) as target:
        json.dump(state, target)


def acquire_lock(handle):
    if fcntl:
        fcntl.flock(handle, fcntl.LOCK_EX)
        return
    handle.seek(0)
    msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, WINDOWS_LOCK_BYTES)


def release_lock(handle):
    if fcntl:
        fcntl.flock(handle, fcntl.LOCK_UN)
        return
    handle.seek(0)
    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, WINDOWS_LOCK_BYTES)


@contextlib.contextmanager
def exclusive_lock(lock_file):
    handle = open(lock_file, "w")
    try:
        acquire_lock(handle)
        yield
    finally:
        release_lock(handle)
        handle.close()
