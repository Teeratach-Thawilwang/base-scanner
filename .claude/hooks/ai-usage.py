#!/usr/bin/env python3
# PreToolUse, Stop and SubagentStop hook: write down every API request as it happens,
# and refuse the next tool call once a window's budget is gone.
#
# PreToolUse is the only hook that runs in the gap between two API requests, so it is
# what makes the log live and lets the cap stop a run before the turn ends. Stop and
# SubagentStop are there for the last reply of a turn, the one no tool call follows.
#
# A reply is written to the transcript once per streamed block, every copy carrying the
# same `msg_...` id, and that id is what makes them one request. The transcript is only
# appended to, so each pass reads the bytes added since the last one and nothing else.

import datetime
import glob
import json
import os
import subprocess
import sys
import time

from hook_lib import FILE_ENCODING, exclusive_lock, load_state, logs_directory, read_event, save_state

REQUESTS = "requests"
TOKENS = "tokens"

# --- settings, edit these -------------------------------------------------
# Measured over this project's own transcripts. Five minutes of real work:
#   requests   median 7      p90 18      p95 24      worst 53
#   tokens     median 1.0M   p90 3.1M    p95 4.5M    worst 12.2M
# One hour of real work:
#   requests   median 41     p90 135     p95 144     worst 188
#   tokens     median 5.7M   p90 21.3M   p95 24.8M   worst 38.6M
# Tokens are input, output, cache write and cache read added together, and cache read
# is most of that: a long context is re-read on every turn, which is how a run that
# looks quiet empties a five-hour quota.
FIVE_MINUTE_REQUEST_LIMIT = 60
FIVE_MINUTE_TOKEN_LIMIT = 10_000_000
HOURLY_REQUEST_LIMIT = 150
HOURLY_TOKEN_LIMIT = 30_000_000
NOTIFY_ON_BLOCK = True
# `CLAUDE_RATE_LIMIT_OFF=1 claude` runs that session uncapped and leaves Codex capped.
# RATE_LIMIT_OFF is the pair's master switch, kept because wanting both uncapped at once
# is the common case and wanting one of them is the rare one.
OFF_SWITCH_VARIABLES = ("CLAUDE_RATE_LIMIT_OFF", "RATE_LIMIT_OFF")
USD_TO_THB = 34
# --------------------------------------------------------------------------

# window in minutes, what is counted, how much of it is allowed
BUDGETS = (
    (5, REQUESTS, FIVE_MINUTE_REQUEST_LIMIT),
    (5, TOKENS, FIVE_MINUTE_TOKEN_LIMIT),
    (60, REQUESTS, HOURLY_REQUEST_LIMIT),
    (60, TOKENS, HOURLY_TOKEN_LIMIT),
)

PRICE_PER_MILLION_TOKENS_USD = {
    "claude-fable-5": {"input": 10.00, "output": 50.00, "cache_create": 12.50, "cache_read": 1.00},
    "claude-opus-5": {"input": 5.00, "output": 25.00, "cache_create": 6.25, "cache_read": 0.50},
    "claude-opus-4-8": {"input": 5.00, "output": 25.00, "cache_create": 6.25, "cache_read": 0.50},
    "claude-opus-4-6": {"input": 5.00, "output": 25.00, "cache_create": 6.25, "cache_read": 0.50},
    "claude-sonnet-5": {"input": 3.00, "output": 15.00, "cache_create": 3.75, "cache_read": 0.30},
    "claude-sonnet-4-6": {"input": 3.00, "output": 15.00, "cache_create": 3.75, "cache_read": 0.30},
    "claude-haiku-4-5": {"input": 1.00, "output": 5.00, "cache_create": 1.25, "cache_read": 0.10},
    "gpt-5-mini": {"input": 0.25, "output": 2.00, "cache_create": 0.25, "cache_read": 0.025},
    "gpt-5-nano": {"input": 0.05, "output": 0.40, "cache_create": 0.05, "cache_read": 0.005},
}
FALLBACK_PRICE = PRICE_PER_MILLION_TOKENS_USD["claude-opus-5"]

USAGE_FIELD = {
    "input": "input_tokens",
    "output": "output_tokens",
    "cache_create": "cache_creation_input_tokens",
    "cache_read": "cache_read_input_tokens",
}

PRE_TOOL_USE = "PreToolUse"
CODEX_ROLLOUT_MARKER = '"session_meta"'  # the first record of a Codex rollout, never of a Claude transcript
SETTLE_TIMEOUT_SECONDS = 3.0
SETTLE_STABLE_SECONDS = 0.4
SETTLE_POLL_SECONDS = 0.2

# A transcript met for the first time is history, not a burst: it seeds the budget so the
# cap knows what the last hour cost, and only its newest replies are written to the log.
CATCHUP_SECONDS = 60
MOMENTS_KEPT = 200
LOGGED_IDS_KEPT = 2000

TRANSCRIPT_SUFFIX = ".jsonl"
META_SUFFIX = ".meta.json"
SUBAGENTS_DIRECTORY = "subagents"

LOG_FILENAME = "ai-requests.log"
STATE_FILENAME = ".ai-usage-state.json"
LOCK_FILENAME = ".ai-usage.lock"

NOTIFY_SCRIPT = "notify-done.sh"
NOTIFY_TITLE = "Claude Code hit the API cap"
NOTIFY_COOLDOWN_SECONDS = 120
NOTIFY_TIMEOUT_SECONDS = 5

STALE_TRANSCRIPT_SECONDS = 6 * 60 * 60
SECONDS_PER_MINUTE = 60
TOKENS_PER_MILLION = 1_000_000

REPLY_ID_PREFIX = "msg_"
SYNTHETIC_MODEL = "<synthetic>"  # a message Claude Code wrote itself, nothing was asked of the API
TRANSCRIPT_ID_LENGTH = 12
REPLY_ID_LENGTH = 8
SESSION_ID_LENGTH = 8
STORED_MONEY_DECIMALS = 4
LOGGED_COST_DECIMALS = 2
LOGGED_BREAKDOWN_DECIMALS = 4
FIELD_SEPARATOR = " | "
CONVERSATION_FIELD = "conv="
ID_FIELD = "id="
BLOCKED_EVENT = "event=rate-limit-block"

COMMAND_NAME_OPEN = "<command-name>"
COMMAND_NAME_CLOSE = "</command-name>"

BLOCK_INSTRUCTION = (
    "[rate-limit] BLOCKED: {spent} {metric} in the last {minutes} minutes, the cap is {limit}.\n"
    "Stop here. Do not retry this call, do not reach for a different tool, and do not start a subagent — "
    "every retry is one more request against the same cap.\n"
    "End the turn now with one line saying what you were doing and what is left. "
    "The user has been notified and decides what happens next."
)


def cap_is_on():
    return not any(os.environ.get(name, "") == "1" for name in OFF_SWITCH_VARIABLES)


# This hook prices Anthropic tokens off a Claude transcript. A Codex rollout holds neither:
# its usage lives in token_count events and a ChatGPT plan is metered in credits, not tokens.
# So .codex/hooks.json never calls this, and a rollout arriving anyway is left alone rather
# than scanned for records it cannot contain.
def is_codex_rollout(transcript_path):
    try:
        with open(transcript_path, encoding=FILE_ENCODING, errors="replace") as transcript:
            return CODEX_ROLLOUT_MARKER in transcript.readline()
    except OSError:
        return False


def parse_record(line):
    try:
        record = json.loads(line)
    except ValueError:
        return {}
    return record if isinstance(record, dict) else {}


def is_assistant_reply(record):
    message = record.get("message")
    return (
        isinstance(message, dict)
        and message.get("role") == "assistant"
        and isinstance(message.get("usage"), dict)
        and str(message.get("id", "")).startswith(REPLY_ID_PREFIX)
        and message.get("model") != SYNTHETIC_MODEL
    )


def epoch_of(timestamp):
    try:
        return datetime.datetime.fromisoformat(timestamp.replace("Z", "+00:00")).timestamp()
    except (AttributeError, ValueError):
        return 0


def iso_of(moment):
    stamped = datetime.datetime.fromtimestamp(moment, datetime.timezone.utc)
    return stamped.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def unread_lines(transcript_path, offset):
    with open(transcript_path, "rb") as transcript:
        size = transcript.seek(0, os.SEEK_END)
        start = 0 if offset > size else offset
        transcript.seek(start)
        unread = transcript.read()
    last_break = unread.rfind(b"\n")
    if last_break < 0:
        return [], start
    complete = unread[: last_break + 1]
    return complete.decode(FILE_ENCODING, "replace").splitlines(), start + len(complete)


# Stop fires the moment the turn ends, which can be a beat before the last reply is flushed.
def wait_until_settled(transcript_path):
    deadline = time.monotonic() + SETTLE_TIMEOUT_SECONDS
    size = -1
    unchanged_since = time.monotonic()
    while time.monotonic() < deadline:
        current = os.path.getsize(transcript_path)
        if current != size:
            size, unchanged_since = current, time.monotonic()
        elif time.monotonic() - unchanged_since >= SETTLE_STABLE_SECONDS:
            return
        time.sleep(SETTLE_POLL_SECONDS)


def command_name_in(record):
    message = record.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if record.get("type") != "user" or not isinstance(content, str) or COMMAND_NAME_OPEN not in content:
        return ""
    return content.split(COMMAND_NAME_OPEN, 1)[1].split(COMMAND_NAME_CLOSE, 1)[0].strip()


def read_chunk(lines):
    chunk = {"replies": [], "moments": {}, "title": "", "command": ""}
    for line in lines:
        record = parse_record(line)
        if not record:
            continue
        if record.get("uuid"):
            chunk["moments"][record["uuid"]] = record.get("timestamp", "")
        if record.get("type") == "ai-title" and record.get("aiTitle"):
            chunk["title"] = record["aiTitle"]
        if not chunk["command"]:
            chunk["command"] = command_name_in(record)
        if is_assistant_reply(record):
            chunk["replies"].append(record)
    return chunk


def placeholder_name(transcript_path):
    return os.path.basename(transcript_path).replace(".jsonl", "")[:TRANSCRIPT_ID_LENGTH]


def without_field_separators(name):
    return " ".join(name.replace("|", "/").split())


def agent_type_of(transcript_path):
    try:
        with open(transcript_path[: -len(TRANSCRIPT_SUFFIX)] + META_SUFFIX, encoding=FILE_ENCODING) as meta:
            return json.load(meta).get("agentType", "")
    except (OSError, ValueError):
        return ""


# Claude Code writes no ai-title record for a session whose prompts are all slash commands,
# and none at all for a subagent, which borrows the name of the session that spawned it.
def conversation_name(entry, transcript_path, session_name):
    own = entry["title"] or entry["command"]
    if own:
        return without_field_separators(own)
    if not session_name:
        return placeholder_name(transcript_path)
    agent = agent_type_of(transcript_path)
    return without_field_separators(f"{session_name} ({agent})" if agent else session_name)


# A subagent keeps its own transcript in a folder beside the session's, and the session's own
# file records nothing of what that subagent spent, so every file in the folder gets read too.
def session_transcripts(transcript_path):
    folder = os.path.dirname(transcript_path)
    parent = os.path.dirname(folder) + TRANSCRIPT_SUFFIX
    root = parent if os.path.basename(folder) == SUBAGENTS_DIRECTORY else transcript_path
    subagents = glob.glob(os.path.join(root[: -len(TRANSCRIPT_SUFFIX)], SUBAGENTS_DIRECTORY, "*" + TRANSCRIPT_SUFFIX))
    return [root, *sorted(subagents)]


def price_of(model):
    for known_model in sorted(PRICE_PER_MILLION_TOKENS_USD, key=len, reverse=True):
        if known_model in model:
            return PRICE_PER_MILLION_TOKENS_USD[known_model]
    return FALLBACK_PRICE


def token_counts(usage):
    return {kind: usage.get(field, 0) for kind, field in USAGE_FIELD.items()}


def costs_in_usd(model, tokens):
    price = price_of(model)
    return {kind: count * price[kind] / TOKENS_PER_MILLION for kind, count in tokens.items()}


def converted_to(currency_rate, costs):
    return {kind: round(cost * currency_rate, STORED_MONEY_DECIMALS) for kind, cost in costs.items()}


def total(costs):
    return round(sum(costs.values()), STORED_MONEY_DECIMALS)


def money(thb, usd, decimals):
    return f"[{thb:.{decimals}f}฿, ${usd:.{decimals}f}]"


def short_model_name(model):
    prefix = "claude-"
    return model[len(prefix) :] if model.startswith(prefix) else model


def reply_mode(message):
    blocks = message.get("content", [])
    thinking = any(isinstance(block, dict) and block.get("type") == "thinking" for block in blocks)
    return "thinking" if thinking else "standard"


def elapsed_between(started_at, completed_at):
    if not started_at or not completed_at:
        return "N/A"
    try:
        started = datetime.datetime.fromisoformat(started_at.replace("Z", "+00:00"))
        completed = datetime.datetime.fromisoformat(completed_at.replace("Z", "+00:00"))
    except ValueError:
        return "N/A"
    return f"{(completed - started).total_seconds():.1f}s"


def format_entry(record, conversation, tokens, thb, usd, totals, elapsed):
    message = record["message"]
    return FIELD_SEPARATOR.join(
        [
            f"model={short_model_name(message.get('model', 'unknown'))}",
            f"cost={money(total(thb), total(usd), LOGGED_COST_DECIMALS)}",
            f"cost_total={money(totals['cumulative_thb'], totals['cumulative_usd'], LOGGED_COST_DECIMALS)}",
            f"{CONVERSATION_FIELD}{conversation}",
            f"mode={reply_mode(message)}",
            record.get("timestamp", ""),
            f"input={tokens['input']}",
            f"input_total={tokens['input'] + tokens['cache_create'] + tokens['cache_read']}",
            f"output={tokens['output']}",
            f"output_total={totals['cumulative_output']}",
            f"cache_create={tokens['cache_create']}",
            f"cache_read={tokens['cache_read']}",
            f"cost_input={money(thb['input'], usd['input'], LOGGED_BREAKDOWN_DECIMALS)}",
            f"cost_output={money(thb['output'], usd['output'], LOGGED_BREAKDOWN_DECIMALS)}",
            f"cost_cache_create={money(thb['cache_create'], usd['cache_create'], LOGGED_BREAKDOWN_DECIMALS)}",
            f"cost_cache_read={money(thb['cache_read'], usd['cache_read'], LOGGED_BREAKDOWN_DECIMALS)}",
            f"duration={elapsed}",
            f"{ID_FIELD}{record.get('uuid', '')[:REPLY_ID_LENGTH]}",
        ]
    )


def add_to_running_totals(totals, thb, usd, tokens):
    totals["cumulative_thb"] = round(totals["cumulative_thb"] + total(thb), STORED_MONEY_DECIMALS)
    totals["cumulative_usd"] = round(totals["cumulative_usd"] + total(usd), STORED_MONEY_DECIMALS)
    totals["cumulative_output"] += tokens["output"]


def trimmed(mapping, kept):
    return dict(list(mapping.items())[-kept:])


def empty_entry():
    return {
        "offset": 0,
        "seen_at": 0,
        "title": "",
        "command": "",
        "name": "",
        "spend": {},  # one reply is [epoch seconds, tokens], keyed by the id that makes it one request
        "moments": {},
        "logged_ids": [],
        "cumulative_thb": 0.0,
        "cumulative_usd": 0.0,
        "cumulative_output": 0,
    }


def entry_for(transcripts, transcript_path):
    stored = transcripts.get(transcript_path, {})
    return {key: stored.get(key, default) for key, default in empty_entry().items()}


def remember(entry, chunk):
    entry["title"] = chunk["title"] or entry["title"]
    entry["command"] = entry["command"] or chunk["command"]
    return {**entry["moments"], **chunk["moments"]}


def entries_for_new_replies(entry, chunk, name, log_cutoff, moments):
    logged = []
    for record in chunk["replies"]:
        message = record["message"]
        reply_id = message["id"]
        if reply_id in entry["spend"]:
            continue

        tokens = token_counts(message["usage"])
        spent_at = epoch_of(record.get("timestamp", ""))
        entry["spend"][reply_id] = [spent_at, sum(tokens.values())]
        if spent_at < log_cutoff:
            continue

        costs = costs_in_usd(message.get("model", "unknown"), tokens)
        thb = converted_to(USD_TO_THB, costs)
        usd = converted_to(1, costs)
        add_to_running_totals(entry, thb, usd, tokens)
        elapsed = elapsed_between(moments.get(record.get("parentUuid")), record.get("timestamp", ""))
        entry["logged_ids"] = (entry["logged_ids"] + [record.get("uuid", "")[:REPLY_ID_LENGTH]])[-LOGGED_IDS_KEPT:]
        logged.append((record.get("timestamp", ""), format_entry(record, name, tokens, thb, usd, entry, elapsed)))
    return logged


def write_atomically(path, content):
    temporary_path = f"{path}.tmp"
    with open(temporary_path, "w", encoding=FILE_ENCODING) as target:
        target.write(content)
    os.replace(temporary_path, path)


def field_value(fields, prefix):
    for field in fields:
        if field.startswith(prefix):
            return field[len(prefix) :]
    return ""


def renamed(line, ids, name):
    fields = line.split(FIELD_SEPARATOR)
    if field_value(fields, ID_FIELD) not in ids:
        return line
    return FIELD_SEPARATOR.join(CONVERSATION_FIELD + name if field.startswith(CONVERSATION_FIELD) else field for field in fields)


# The title only arrives once the session has a few turns behind it, so the lines already
# written under a placeholder get the real name put on them here.
def rename_logged_replies(log_file, ids, name):
    if not ids or not os.path.exists(log_file):
        return
    with open(log_file, encoding=FILE_ENCODING) as log:
        logged = log.read().splitlines()
    lines = [renamed(line, set(ids), name) for line in logged]
    if lines == logged:
        return
    write_atomically(log_file, "".join(line + "\n" for line in lines))


def append_lines(log_file, lines):
    if not lines:
        return
    with open(log_file, "a", encoding=FILE_ENCODING) as log:
        for line in lines:
            log.write(line + "\n")


def still_in_budget(spend, oldest_kept):
    return {reply_id: entry for reply_id, entry in spend.items() if isinstance(entry, list) and len(entry) == 2 and entry[0] >= oldest_kept}


def longest_budget_seconds():
    return max(minutes for minutes, _, _ in BUDGETS) * SECONDS_PER_MINUTE


def pruned(transcripts, now):
    oldest_kept = now - longest_budget_seconds()
    kept = {}
    for transcript_path, entry in transcripts.items():
        entry["spend"] = still_in_budget(entry["spend"], oldest_kept)
        if entry["spend"] or now - entry["seen_at"] < STALE_TRANSCRIPT_SECONDS:
            kept[transcript_path] = entry
    return kept


def spent_since(transcripts, oldest_counted):
    spends = [spend for entry in transcripts.values() for spend in entry["spend"].values() if spend[0] >= oldest_counted]
    return {REQUESTS: len(spends), TOKENS: sum(spend[1] for spend in spends)}


def spend_per_budget(transcripts, now):
    windows = {minutes: spent_since(transcripts, now - minutes * SECONDS_PER_MINUTE) for minutes, _, _ in BUDGETS}
    return [(minutes, metric, windows[minutes][metric], limit) for minutes, metric, limit in BUDGETS]


def first_breach(spend):
    for budget in spend:
        if budget[2] >= budget[3]:
            return budget
    return None


def readable(metric, amount):
    if metric != TOKENS:
        return f"{amount:,}"
    return f"{amount / TOKENS_PER_MILLION:.1f}M"


def due_for_notice(state, session_id, now):
    if not NOTIFY_ON_BLOCK:
        return False
    notified_at = state.setdefault("notified_at", {})
    if now - notified_at.get(session_id, 0) < NOTIFY_COOLDOWN_SECONDS:
        return False
    notified_at[session_id] = now
    return True


def notify(breach):
    minutes, metric, spent, limit = breach
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), NOTIFY_SCRIPT)
    status = f"{readable(metric, spent)} {metric} in {minutes} min, cap is {readable(metric, limit)}. Tool calls blocked — press Esc."
    try:
        subprocess.run(["bash", script, NOTIFY_TITLE, status], stdin=subprocess.DEVNULL, timeout=NOTIFY_TIMEOUT_SECONDS, check=False)
    except (OSError, subprocess.SubprocessError):
        pass


def blocked_line(breach, spend, tool_name, session_id, now):
    return FIELD_SEPARATOR.join(
        [
            BLOCKED_EVENT,
            f"breached={breach[0]}m/{breach[1]}",
            *(f"{minutes}m/{metric}={spent}/{limit}" for minutes, metric, spent, limit in spend),
            f"tool={tool_name}",
            f"session={session_id[:SESSION_ID_LENGTH]}",
            iso_of(now),
        ]
    )


def absorb(transcripts, transcript_path, log_file, now, session_name):
    first_sight = transcript_path not in transcripts
    entry = entry_for(transcripts, transcript_path)

    lines, offset = unread_lines(transcript_path, entry["offset"])
    chunk = read_chunk(lines)
    moments = remember(entry, chunk)
    entry["offset"] = offset
    entry["seen_at"] = now

    name = conversation_name(entry, transcript_path, session_name)
    logged = entries_for_new_replies(entry, chunk, name, now - CATCHUP_SECONDS if first_sight else 0, moments)
    entry["moments"] = trimmed(moments, MOMENTS_KEPT)
    if name != entry["name"]:
        rename_logged_replies(log_file, entry["logged_ids"], name)
        entry["name"] = name
    append_lines(log_file, [line for _, line in sorted(logged, key=lambda logged_reply: logged_reply[0])])

    transcripts[transcript_path] = entry
    return name


def account_for(state, transcript_path, log_file, now):
    transcripts = state.setdefault("transcripts", {})
    session_name = ""
    for path in session_transcripts(transcript_path):
        if os.path.isfile(path):
            name = absorb(transcripts, path, log_file, now, session_name)
            session_name = session_name or name
    state["transcripts"] = pruned(transcripts, now)
    return spend_per_budget(state["transcripts"], now)


def main():
    event = read_event()
    if event.get("stop_hook_active"):
        return 0

    transcript_path = os.path.expanduser(event.get("transcript_path", ""))
    if not os.path.isfile(transcript_path) or is_codex_rollout(transcript_path):
        return 0

    on_tool_call = event.get("hook_event_name") == PRE_TOOL_USE
    if not on_tool_call:
        wait_until_settled(transcript_path)

    logs_dir = logs_directory(event)
    os.makedirs(logs_dir, exist_ok=True)
    log_file = os.path.join(logs_dir, LOG_FILENAME)
    state_file = os.path.join(logs_dir, STATE_FILENAME)
    session_id = str(event.get("session_id", ""))
    now = time.time()

    with exclusive_lock(os.path.join(logs_dir, LOCK_FILENAME)):
        state = load_state(state_file)
        spend = account_for(state, transcript_path, log_file, now)
        breach = first_breach(spend) if on_tool_call and cap_is_on() else None
        announce = bool(breach) and due_for_notice(state, session_id, now)
        if breach:
            append_lines(log_file, [blocked_line(breach, spend, str(event.get("tool_name", "")), session_id, now)])
        save_state(state_file, state)

    if not breach:
        return 0
    if announce:
        notify(breach)

    minutes, metric, spent, limit = breach
    print(BLOCK_INSTRUCTION.format(spent=readable(metric, spent), metric=metric, minutes=minutes, limit=readable(metric, limit)), file=sys.stderr)
    return 2


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as failure:  # a broken accountant must never be the thing that wedges a session
        print(f"[ai-usage] skipped, {failure}", file=sys.stderr)
        sys.exit(0)
