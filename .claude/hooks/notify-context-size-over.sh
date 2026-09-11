#!/usr/bin/env bash
# UserPromptSubmit hook: warn the agent when the live context window is close to full.
#
# Reads the real token count out of the transcript, which is the same number /context
# reports. Do not measure the transcript file size: tool_result bytes stay in the file
# forever after they leave the context window, and /compact resets the context without
# shrinking the file, so bytes overstate the context by 20x to 55x on long sessions and
# the warning never stops firing.
#
# Two transcript formats, one script. Claude writes assistant records carrying
# message.usage. Codex writes event_msg records of type token_count, and those carry
# model_context_window as well, so the Codex threshold is a share of the real window
# instead of a number written down here.

set -euo pipefail

CLAUDE_THRESHOLD_TOKENS=350000  # Claude's transcript never states the window, so this is fixed
CODEX_WINDOW_SHARE=70           # percent of model_context_window

payload=$(cat)
transcript_path=$(printf '%s' "$payload" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("transcript_path",""))' 2>/dev/null || echo "")
session_id=$(printf '%s' "$payload" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("session_id",""))' 2>/dev/null || echo "")

[[ -z "$transcript_path" || ! -f "$transcript_path" ]] && exit 0

# One line back: "<runtime> <context_tokens> <window_or_0>".
# Claude: the last main-thread assistant turn wins, sidechain entries are subagent
# contexts, not this one. Codex: the last token_count event wins.
read -r runtime context_tokens window <<<"$(python3 - "$transcript_path" <<'PY' 2>/dev/null || echo "unknown 0 0"
import json, sys

claude_total = 0
codex_total = 0
codex_window = 0

with open(sys.argv[1], errors="replace") as fh:
    for line in fh:
        if '"usage"' not in line and '"token_count"' not in line:
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue

        payload = entry.get("payload")
        if isinstance(payload, dict) and payload.get("type") == "token_count":
            info = payload.get("info") or {}
            usage = info.get("last_token_usage") or {}
            if usage:
                codex_total = usage.get("total_tokens") or (
                    usage.get("input_tokens", 0) + usage.get("output_tokens", 0)
                )
                codex_window = info.get("model_context_window") or codex_window
            continue

        if entry.get("isSidechain") or entry.get("type") != "assistant":
            continue
        usage = (entry.get("message") or {}).get("usage") or {}
        if not usage:
            continue
        claude_total = (
            usage.get("input_tokens", 0)
            + usage.get("cache_read_input_tokens", 0)
            + usage.get("cache_creation_input_tokens", 0)
            + usage.get("output_tokens", 0)
        )

if codex_total:
    print("codex", codex_total, codex_window)
elif claude_total:
    print("claude", claude_total, 0)
else:
    print("unknown", 0, 0)
PY
)"

[[ "$runtime" == "unknown" || "$context_tokens" -le 0 ]] && exit 0

if [[ "$runtime" == "codex" ]]; then
  [[ "$window" -gt 0 ]] || exit 0
  threshold=$(( window * CODEX_WINDOW_SHARE / 100 ))
  state_dir="$HOME/.codex/state/context-warn"
  compact_hint="Ask the user whether to run /compact now, and do not ask again until the context grows significantly."
else
  threshold=$CLAUDE_THRESHOLD_TOKENS
  state_dir="$HOME/.claude/state/context-warn"
  compact_hint="Before answering this prompt, use AskUserQuestion to ask the user whether they want to run /compact now. Options: [รัน /compact] / [ไม่ต้อง ทำต่อเลย]. If they decline, do not ask again until context grows significantly."
fi

[[ "$context_tokens" -lt "$threshold" ]] && exit 0

# Avoid spamming: warn once per session, then again only after another 25% of growth.
# A drop means /compact ran, so forget the old mark and let the next crossing warn.
mkdir -p "$state_dir"
state_file="$state_dir/${session_id:-default}"
last_warn=0
[[ -f "$state_file" ]] && last_warn=$(cat "$state_file" 2>/dev/null || echo 0)
[[ "$context_tokens" -lt "$last_warn" ]] && last_warn=0

growth_threshold=$(( last_warn + last_warn / 4 ))
if [[ "$last_warn" -gt 0 && "$context_tokens" -lt "$growth_threshold" ]]; then
  exit 0
fi

echo "$context_tokens" > "$state_file"

cat <<EOF
[context-size-warning] Context is at ~${context_tokens} tokens — over the ${threshold} threshold. ${compact_hint}
EOF
