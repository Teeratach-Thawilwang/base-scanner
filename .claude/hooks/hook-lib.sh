#!/usr/bin/env bash
# Sourced by the hooks in this folder, never executed on its own.
# The JSON each event puts on stdin: https://code.claude.com/docs/en/hooks
#
# Codex sends the same events under the same field names, so one script serves both.
# Where the two differ it is the tool, never the envelope: Claude writes a file with
# Write or Edit and names it in tool_input.file_path, Codex writes it with apply_patch
# and names it inside the patch body. hook_edited_files reads either one.

# A hook whose stdin is never closed would otherwise sit here forever as an orphan.
# macOS ships no `timeout`, and Homebrew coreutils names it `gtimeout`, so both are optional.
hook_read_stdin() {
  if command -v timeout >/dev/null 2>&1; then
    timeout 10 cat
  elif command -v gtimeout >/dev/null 2>&1; then
    gtimeout 10 cat
  else
    cat
  fi
}

hook_require_node() {
  command -v node >/dev/null 2>&1 && return 0
  printf '%s: node is not on PATH, so this hook checked nothing.\n' "$1" >&2
  return 1
}

# One line out per dotted field asked for, in order, empty when the field is absent.
# node parses it because Windows paths arrive escaped and sed would mangle them.
hook_read_fields() {
  local payload=$1
  shift
  printf '%s' "$payload" | node -e '
    const wanted = process.argv.slice(1);
    let raw = "";
    process.stdin.on("data", (chunk) => (raw += chunk)).on("end", () => {
      let payload = {};
      try {
        payload = JSON.parse(raw);
      } catch {}
      for (const field of wanted) {
        const value = field
          .split(".")
          .reduce((node, key) => (node == null ? node : node[key]), payload);
        console.log(value == null || typeof value === "object" ? "" : String(value));
      }
    });
  ' "$@"
}

# Windows sends C:\a\b, macOS sends /a/b. Everything downstream sees forward slashes only.
hook_normalize_path() {
  case $1 in
    [A-Za-z]:*) printf '%s' "$1" | tr '\\' '/' ;;
    *) printf '%s' "$1" ;;
  esac
}

# The nearest ancestor holding a package.json is the project that owns the file.
hook_find_project_root() {
  local dir=$1 parent
  while [ -n "$dir" ]; do
    if [ -f "$dir/package.json" ]; then
      printf '%s' "$dir"
      return 0
    fi
    parent=$(dirname "$dir")
    # dirname stops shrinking at "/" on macOS and at "." on Windows.
    [ "$parent" = "$dir" ] && return 1
    dir=$parent
  done
  return 1
}

# Every file a Write, an Edit or an apply_patch touched, one absolute path per line.
# Claude puts the path in tool_input.file_path. Codex puts it in the apply_patch body,
# in the `*** Update File: x` lines, and the body's own field name has moved before
# (input, patch, changes), so every string in tool_input is scanned rather than one.
hook_edited_files() {
  printf '%s' "$1" | node -e '
    let raw = "";
    process.stdin.on("data", (chunk) => (raw += chunk)).on("end", () => {
      let input = {};
      try {
        input = JSON.parse(raw).tool_input ?? {};
      } catch {}
      const paths = new Set();

      // apply_patch headers, whichever field the patch body arrived in.
      const header = /^\*\*\* (?:Add|Update|Move) File: (.+)$/gm;
      const scan = (value) => {
        if (typeof value === "string") {
          for (const [, path] of value.matchAll(header)) paths.add(path.trim());
        } else if (Array.isArray(value)) {
          value.forEach(scan);
        } else if (value && typeof value === "object") {
          Object.values(value).forEach(scan);
        }
      };

      for (const key of ["file_path", "path", "notebook_path"]) {
        if (typeof input[key] === "string" && input[key]) paths.add(input[key]);
      }
      // `changes` keyed by path is the other shape apply_patch has shipped.
      if (input.changes && typeof input.changes === "object" && !Array.isArray(input.changes)) {
        Object.keys(input.changes).forEach((path) => paths.add(path));
      }
      scan(input);

      for (const path of paths) console.log(path);
    });
  '
}

# CLAUDE_PROJECT_DIR is Claude's alone. Codex hands the hook the session cwd instead.
hook_project_root() {
  if [ -n "${CLAUDE_PROJECT_DIR:-}" ]; then
    printf '%s' "$CLAUDE_PROJECT_DIR"
    return 0
  fi
  git rev-parse --show-toplevel 2>/dev/null && return 0
  pwd
}
