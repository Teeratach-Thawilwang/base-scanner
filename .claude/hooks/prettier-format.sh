#!/usr/bin/env bash
# PostToolUse hook: run the owning project's Prettier over the files just written.
# Silence means formatted, or the file belongs to no project that asked for Prettier.
# Exit 2 is the only way a hook's stderr reaches the agent, exit 0 is silence.
#
# Claude fires this on Write and Edit, one absolute path per call. Codex fires it on
# apply_patch, which can carry several files at once and writes them relative to the
# session cwd, so the loop and the cwd fallback below are not decoration.

set -u

. "$(dirname "${BASH_SOURCE[0]}")/hook-lib.sh"

INPUT=$(hook_read_stdin)
hook_require_node "prettier-format" || exit 2

FAILURES=""

format_one() {
  FILE=$(hook_normalize_path "$1")
  case "$FILE" in
    /* | [A-Za-z]:/*) ;;
    *) FILE="$(pwd)/$FILE" ;;  # apply_patch paths are relative to the session cwd
  esac
  [ -f "$FILE" ] || return 0

  ROOT=$(hook_find_project_root "$(dirname "$FILE")") || return 0

  PRETTIER="$ROOT/node_modules/.bin/prettier"
  if [ ! -f "$PRETTIER" ]; then
    # A project that never asked for Prettier is fine. One that asked and lacks it is not.
    grep -q '"prettier"' "$ROOT/package.json" || return 0
    FAILURES="${FAILURES}prettier-format: $ROOT declares prettier but node_modules/.bin/prettier is missing. Run npm install there."$'\n'
    return 0
  fi

  # --ignore-unknown leaves a file Prettier has no parser for exactly as it is.
  OUTPUT=$("$PRETTIER" --ignore-unknown --write "$FILE" 2>&1) && return 0
  FAILURES="${FAILURES}prettier failed on $FILE"$'\n'"$OUTPUT"$'\n'
}

while IFS= read -r edited; do
  [ -n "$edited" ] && format_one "$edited"
done <<< "$(hook_edited_files "$INPUT")"

[ -z "$FAILURES" ] && exit 0
printf '%s' "$FAILURES" >&2
exit 2
