#!/usr/bin/env bash
# Point Codex at the skills Claude already has. One copy on disk, two agents reading it.
#
# Codex looks for project skills in .codex/skills and Claude keeps them in .claude/skills,
# so the folder is a link, not a second copy. The link is made per machine instead of being
# committed: git on Windows needs core.symlinks on and Developer Mode to check a symlink
# out, and a silently broken link is worse than a missing one.
#
# Run once per clone:  bash .codex/link-skills.sh

set -euo pipefail

root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
link="$root/.codex/skills"
target="$root/.claude/skills"

[ -d "$target" ] || { printf 'link-skills: %s does not exist.\n' "$target" >&2; exit 1; }

# -n so an existing link is replaced instead of the new one landing inside it.
ln -sfn ../.claude/skills "$link"

count=$(find "$link/" -maxdepth 2 -name SKILL.md | wc -l | tr -d ' ')
printf 'link-skills: .codex/skills -> ../.claude/skills, %s skills visible to Codex.\n' "$count"
