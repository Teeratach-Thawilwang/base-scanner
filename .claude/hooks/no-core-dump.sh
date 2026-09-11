#!/bin/bash
# Sourced by every non-interactive bash through BASH_ENV (wired in .claude/settings.json).
#
# Why: MSYS bash (Git for Windows) crashes intermittently inside its signal machinery and writes
# bash.exe.stackdump into the process cwd — the repo root. The dump is written only while the core
# limit allows it, so dropping the limit to 0 removes the file without hiding anything we act on.
# Harmless on macOS/Linux: it only turns off core files for hook shells.
ulimit -c 0 2>/dev/null || true
