#!/usr/bin/env bash
# Apply a whole commit plan in ONE call: n commits, still n commits, but one Bash turn.
# The plan arrives on stdin, so no plan file has to be written first.
#
# Usage (run it by the path the skill prints; this is relative to the skill's directory):
#   bash scripts/commit-apply.sh [repo] <<'PLAN'
#   ### FILES
#   src/auth/token.ts
#   src/auth/session.ts
#   ### MESSAGE
#   feat(auth): rotate refresh tokens on every use
#
#   A stolen refresh token stayed valid until it expired. Rotating on use makes a
#   replayed token detectable and lets the server revoke the whole family.
#   ### COMMIT
#   ### FILES
#   docs/auth.md
#   ### MESSAGE
#   docs(auth): document the rotation flow
#
#   The rotation rules only lived in the code; on-call had no reference.
#   ### COMMIT
#   PLAN
#
# One path per line (so spaces in names are safe). Files only, never a directory. A rename is
# TWO paths, the old one and the new one: staging the new path alone leaves the old file in the
# commit and its deletion in the working tree.
#
# Why bundling n commits into one call is safe here. This script:
#   * rejects a malformed plan outright, rather than committing a truncated message
#   * refuses to start while a change exists ONLY in the index, which its `git reset` would destroy
#   * validates every planned path against the working tree BEFORE commit 1 runs
#   * refuses secret-looking paths        (override: ALLOW_SENSITIVE=1, only once the user agrees)
#   * `git reset` once up front, so nothing pre-staged rides into the wrong commit
#   * stages the listed paths only        (never `git add -A` / `.`)
#   * re-reads the index after each `add` and refuses if it is not exactly what the plan listed
#   * STOPS DEAD on the first failure (hook, conflict, empty commit): the remaining commits do
#     not run, and it prints what landed, what is still staged, and what to do next
#   * never --no-verify, never --amend, never push
#
# Runs on macOS, Linux, and Windows (Git Bash).

set -u
export LC_ALL=C # `sort` and `comm` below must agree on collation, whatever the user's locale is

# An inherited GIT_DIR or GIT_WORK_TREE (a hook, a wrapper script, an IDE) silently outranks
# `git -C`, and this script would then commit into a repo the caller never named.
unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE
# Every path in the plan is a filename, never a pattern. Without this, `git add 'docs/a[1].md'`
# also stages `docs/a1.md`, and the commit carries a file the plan never listed.
export GIT_LITERAL_PATHSPECS=1

REPO="$(git -C "${1:-.}" rev-parse --show-toplevel 2>/dev/null)" || {
  echo "NOT_A_GIT_REPO: ${1:-.}"
  exit 1
}

# core.quotepath=false so a Thai or accented path prints as itself, the same string the plan
# carries and the survey printed.
g() { git -C "$REPO" -c core.quotepath=false "$@"; }
has_head() { g rev-parse --verify -q HEAD >/dev/null 2>&1; }

# What the plan is judged against. A repo with no commit yet has no HEAD to diff, so the base is
# git's empty tree and the first commit's files read as new files instead of as nothing at all.
EMPTY_TREE="$(g hash-object -t tree /dev/null)" # `g`, so a SHA-256 repo hashes its own empty tree
if has_head; then
  BASE=HEAD
  BASE_LABEL=HEAD
else
  BASE="$EMPTY_TREE"
  BASE_LABEL="the empty tree (no commit yet)"
fi

# A merge, rebase, cherry-pick or revert in progress owns the index. This script resets the index
# and then commits path by path, which would either leave the other side of the merge out of the
# merge commit or record a commit that silently contradicts it. Name the operation, refuse, stop.
GIT_DIR_ABS="$(g rev-parse --absolute-git-dir 2>/dev/null)" || GIT_DIR_ABS="$REPO/.git"
operation_in_progress() {
  [ -e "$GIT_DIR_ABS/MERGE_HEAD" ] && echo "a merge" && return 0
  [ -e "$GIT_DIR_ABS/CHERRY_PICK_HEAD" ] && echo "a cherry-pick" && return 0
  [ -e "$GIT_DIR_ABS/REVERT_HEAD" ] && echo "a revert" && return 0
  [ -d "$GIT_DIR_ABS/rebase-merge" ] || [ -d "$GIT_DIR_ABS/rebase-apply" ] && echo "a rebase" && return 0
  [ -n "$(g ls-files --unmerged)" ] && echo "an unresolved conflict" && return 0
  return 1
}

# What is in the index right now, one path per line. HEAD starts existing the moment commit 1
# lands, so this decides per call rather than reusing BASE.
staged_paths() {
  if has_head; then
    g diff --cached --name-only --no-renames -z
  else
    g diff --cached "$EMPTY_TREE" --name-only --no-renames -z
  fi | tr '\0' '\n' | awk 'length($0)'
}

TMP="$(mktemp -d)" || {
  echo "FAILED: mktemp -d — no writable temp directory, so nothing was staged or committed"
  exit 1
}
trap 'rm -rf "$TMP"' EXIT

# Same guard as commit-survey.sh, kept identical: the basename decides most of it, the directory
# decides the rest. These must never be staged without the user saying so.
secret() {
  case "${1##*/}" in
    *.example | *.sample | *.template) return 1 ;;
    .env | .env.* | .envrc | *.pem | *.key | *.p12 | *.pfx | *.jks | *.keystore) return 0 ;;
    id_rsa* | id_ed25519* | .npmrc | .netrc | .htpasswd | *serviceAccount*) return 0 ;;
    *credential* | *secret* | *.kdbx) return 0 ;;
  esac
  case "$1" in
    secret*/* | */secret*/* | credential*/* | */credential*/* | .ssh/* | */.ssh/*) return 0 ;;
  esac
  return 1
}

# ---------- 1. parse the plan (stdin) ----------
# The three markers are recognised anywhere, so a message body that happens to contain a line
# reading exactly `### COMMIT` used to end the message there and commit the truncated half with
# exit 0. Every marker is now checked against the block it may legally appear in, and any text
# that lands outside a block is an error instead of being dropped.
n=0
mode=""
line="" # `read` never assigns it when stdin is closed, and the loop test reads it under `set -u`
lineno=0
malformed=0
reject_line() {
  echo "PLAN ERROR: line $lineno: $1"
  malformed=1
}
while IFS= read -r line || [ -n "$line" ]; do
  lineno=$((lineno + 1))
  line="${line%$'\r'}" # a plan written on Windows arrives CRLF; the CR is never part of a path
  case "$line" in
    '### FILES')
      [ "$mode" = files ] || [ "$mode" = msg ] && reject_line "'### FILES' inside commit $n, close it with '### COMMIT' first"
      n=$((n + 1))
      mode=files
      : >"$TMP/$n.files"
      : >"$TMP/$n.msg"
      continue
      ;;
    '### MESSAGE')
      [ "$mode" = files ] || reject_line "'### MESSAGE' with no '### FILES' block open"
      mode=msg
      continue
      ;;
    '### COMMIT')
      [ "$mode" = msg ] || reject_line "'### COMMIT' with no '### MESSAGE' block open"
      mode=closed
      continue
      ;;
  esac
  case "$mode" in
    files) [ -n "$line" ] && printf '%s\n' "$line" >>"$TMP/$n.files" ;;
    msg) printf '%s\n' "$line" >>"$TMP/$n.msg" ;;
    *) [ -n "$line" ] && reject_line "text outside every block: '$line'" ;;
  esac
done

[ "$mode" = files ] || [ "$mode" = msg ] && {
  echo "PLAN ERROR: commit $n was never closed with '### COMMIT' — a truncated plan is not applied"
  malformed=1
}

[ "$n" -eq 0 ] && {
  echo "EMPTY PLAN — expected '### FILES / ### MESSAGE / ### COMMIT' blocks on stdin"
  exit 1
}

[ "$malformed" -eq 1 ] && {
  echo
  echo "A message body may not contain a line that is exactly '### FILES', '### MESSAGE' or"
  echo "'### COMMIT'. Reword that line, or indent it, then re-run."
  echo "ABORTED — nothing was staged, nothing was committed."
  exit 1
}

# ---------- 2. refuse to run over an index this script does not own ----------
op="$(operation_in_progress)" && {
  echo "REFUSED: $op is in progress, so the index is not this script's to reset."
  echo "Nothing was staged, nothing was committed."
  echo "Finish or abort it with plain git first ('git merge --continue', 'git merge --abort',"
  echo "'git rebase --continue', 'git cherry-pick --abort', ...), then re-run."
  exit 1
}

# ---------- 3. refuse to run over a change that lives only in the index ----------
# Step 5 starts with `git reset`, which rewrites the index from BASE. A path whose INDEX copy
# differs from BASE while its WORKING TREE copy does not was staged and then reverted on disk:
# the reset would drop that version with nothing left on disk to stage again, and the commit
# would silently be missing it. Hand it back to the user instead of destroying it.
g diff "$BASE" --name-only --no-renames -z | tr '\0' '\n' | awk 'length($0)' >"$TMP/worktree"
staged_paths >"$TMP/staged"
: >"$TMP/index.only"
while IFS= read -r f; do
  grep -qxF -- "$f" "$TMP/worktree" || printf '%s\n' "$f" >>"$TMP/index.only"
done <"$TMP/staged"

[ -s "$TMP/index.only" ] && {
  echo "REFUSED: these paths exist ONLY in the index, their working tree copy matches $BASE_LABEL:"
  sed 's/^/  /' "$TMP/index.only"
  echo
  echo "This script starts with 'git reset', which would throw that staged version away."
  echo "Nothing was staged, nothing was committed. Per path, pick one and re-run:"
  echo "  git checkout-index -f -- <path>   # put the staged version back in the working tree"
  echo "  git restore --staged -- <path>    # drop the staged version on purpose"
  exit 1
}

# ---------- 4. validate against the working tree — before anything is touched ----------
# The universe of committable paths: tracked changes + untracked files. --no-renames is what
# keeps this list true after the `git reset` in step 5. With detection on, a staged `git mv`
# prints only the new path, while the reset turns it back into delete(old) + untracked(new).
# The plan could then never name the old path, and the deletion would be left out of the commit
# without a word.
cp "$TMP/worktree" "$TMP/changed"
g ls-files --others --exclude-standard -z | tr '\0' '\n' | awk 'length($0)' >>"$TMP/changed"

bad=0
i=0
: >"$TMP/seen" # one path belongs to one commit, so a repeat is a plan error
while [ "$i" -lt "$n" ]; do
  i=$((i + 1))
  [ -s "$TMP/$i.files" ] || {
    echo "PLAN ERROR: commit $i lists no files"
    bad=1
  }
  grep -q '[^[:space:]]' "$TMP/$i.msg" || {
    echo "PLAN ERROR: commit $i has no message — git would reject the commit half way through"
    bad=1
  }
  while IFS= read -r f; do
    if secret "$f" && [ "${ALLOW_SENSITIVE:-0}" != "1" ]; then
      echo "REFUSED: '$f' (commit $i) looks like a secret — ask the user first, then re-run with ALLOW_SENSITIVE=1"
      bad=1
      continue
    fi
    if grep -qxF -- "$f" "$TMP/seen"; then
      echo "PLAN ERROR: '$f' (commit $i) is listed more than once, one path belongs to one commit"
      bad=1
      continue
    fi
    printf '%s\n' "$f" >>"$TMP/seen"
    # Git's own list decides first: a submodule is a single entry there while being a directory
    # on disk, so testing for a directory before consulting it would reject every submodule bump.
    if grep -qxF -- "$f" "$TMP/changed"; then
      continue
    fi
    if [ -d "$REPO/$f" ]; then
      echo "PLAN ERROR: '$f' (commit $i) is a directory — list its files individually"
    else
      echo "PLAN ERROR: '$f' (commit $i) has no changes to commit — check the path against the survey"
    fi
    bad=1
  done <"$TMP/$i.files"
done

[ "$bad" -eq 1 ] && {
  echo
  echo "ABORTED — nothing was staged, nothing was committed. Fix the plan and re-run."
  exit 1
}

# ---------- 5. execute ----------
g reset -q || {
  echo "FAILED: git reset (could not unstage) — nothing committed"
  exit 1
}

committed=0
abort() {
  echo
  if [ "$1" -lt "$n" ]; then
    echo "=== ABORTED at commit $1/$n, commits $(($1 + 1))..$n never ran ==="
  else
    echo "=== ABORTED at commit $1/$n, the last one, so nothing was skipped ==="
  fi
  if [ "$committed" -gt 0 ]; then
    echo "LANDED — commits 1..$committed (do NOT re-run these):"
    g log -"$committed" --oneline
  else
    echo "LANDED: none"
  fi
  echo "STILL STAGED right now (in no commit):"
  if [ -n "$(staged_paths)" ]; then
    staged_paths
  else
    echo "  (nothing)"
  fi
  echo
  echo "Fix the root cause, then re-run this script with commits $1..$n only."
  echo "Never --amend a landed commit, never --no-verify."
  exit 1
}

# `git add <path>` takes a pathspec, not a filename: a case-folding filesystem, a glob character
# in a name, or a content filter that normalises a file to no change at all can each leave the
# index holding something other than what the plan listed. Compare them while it is still free.
verify_staged_matches_plan() {
  sort "$TMP/$1.files" >"$TMP/$1.want"
  staged_paths | sort >"$TMP/$1.got"
  cmp -s "$TMP/$1.want" "$TMP/$1.got" && return 0
  echo "REFUSED: the index is not what commit $1 planned, so this commit was not made."
  echo "  planned, not staged:"
  comm -23 "$TMP/$1.want" "$TMP/$1.got" | sed 's/^/    /'
  echo "  staged, not planned:"
  comm -13 "$TMP/$1.want" "$TMP/$1.got" | sed 's/^/    /'
  return 1
}

# A pre-commit hook stages files of its own AFTER the index was checked, and they land in the
# commit anyway. Read the commit back: what is reported as done has to be what was planned.
verify_commit_matches_plan() {
  g diff-tree --root --no-commit-id --name-only -r HEAD | sort >"$TMP/$1.landed"
  cmp -s "$TMP/$1.want" "$TMP/$1.landed" && return 0
  echo "REFUSED: commit $1 LANDED, but not with the files the plan named (a hook, most likely)."
  echo "  planned, not in the commit:"
  comm -23 "$TMP/$1.want" "$TMP/$1.landed" | sed 's/^/    /'
  echo "  in the commit, never planned:"
  comm -13 "$TMP/$1.want" "$TMP/$1.landed" | sed 's/^/    /'
  return 1
}

i=0
while [ "$i" -lt "$n" ]; do
  i=$((i + 1))
  files=()
  while IFS= read -r f; do files+=("$f"); done <"$TMP/$i.files"
  echo "── commit $i/$n: ${#files[@]} file(s)"
  g add -- "${files[@]}" || abort "$i"
  verify_staged_matches_plan "$i" || abort "$i"
  # --cleanup=whitespace is the default for -F, but only until a repo sets commit.cleanup=strip,
  # which deletes every body line starting with '#' from the message that lands.
  g commit --cleanup=whitespace -F "$TMP/$i.msg" || abort "$i"
  committed=$((committed + 1))
  verify_commit_matches_plan "$i" || abort "$i"
done

echo
echo "=== DONE — $n commit(s) ==="
g log -"$n" --oneline
[ -z "$(g branch --show-current)" ] && {
  echo
  echo "=== ON A DETACHED HEAD — these commits are on no branch and the next checkout orphans"
  echo "=== them. Tell the user, they decide what to do about it."
}
echo
echo "=== LEFT IN THE WORKING TREE (uncommitted on purpose?) ==="
g status --short
exit 0
