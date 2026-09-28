#!/usr/bin/env python3
import datetime
import glob
import json
import os
import sys
import time

from hook_lib import FILE_ENCODING, exclusive_lock, load_state, logs_directory, read_event, save_state

USD_TO_THB = 34

PRICE_PER_MILLION_TOKENS_USD = {
    "claude-fable-5": {"input": 10.00, "output": 50.00, "cache_create": 12.50, "cache_read": 1.00},
    "claude-opus-5-5": {"input": 4.00, "output": 20.00, "cache_create": 5.00, "cache_read": 0.20},
    "claude-opus-5": {"input": 5.00, "output": 25.00, "cache_create": 6.25, "cache_read": 0.50},
    "claude-opus-4-8": {"input": 5.00, "output": 25.00, "cache_create": 6.25, "cache_read": 0.50},
    "claude-opus-4-6": {"input": 5.00, "output": 25.00, "cache_create": 6.25, "cache_read": 0.50},
    "claude-sonnet-5-5": {"input": 2.00, "output": 10.00, "cache_create": 2.50, "cache_read": 0.20},
    "claude-sonnet-5": {"input": 2.00, "output": 10.00, "cache_create": 2.50, "cache_read": 0.20},
    "claude-sonnet-4-6": {"input": 3.00, "output": 15.00, "cache_create": 3.75, "cache_read": 0.30},
    "claude-haiku-4-5": {"input": 1.00, "output": 5.00, "cache_create": 1.25, "cache_read": 0.10},
    "gpt-5-mini": {"input": 0.25, "output": 2.00, "cache_create": 0.25, "cache_read": 0.025},
    "gpt-5-nano": {"input": 0.05, "output": 0.40, "cache_create": 0.05, "cache_read": 0.005},
    # OpenAI API-equivalent rates; the ChatGPT subscription does not bill per request.
    "gpt-6-sol": {"input": 2.00, "output": 10.00, "cache_create": 2.50, "cache_read": 0.20},
    "gpt-5.6-terra": {"input": 2.00, "output": 12.00, "cache_create": 2.50, "cache_read": 0.20},
    "gpt-6-luna": {"input": 0.10, "output": 0.50, "cache_create": 0.125, "cache_read": 0.01},
    # Implicit cache writes are billed at the input miss rate on OpenRouter.
    "glm-5.3-flash": {"input": 0.15, "output": 0.50, "cache_create": 0.15, "cache_read": 0.03},
    "qwen3.8-flash": {"input": 0.15, "output": 0.47, "cache_create": 0.15, "cache_read": 0.016},
}
LONG_CONTEXT_PRICE_PER_MILLION_TOKENS_USD = {
    "gpt-6-sol": {"input": 4.00, "output": 15.00, "cache_create": 5.00, "cache_read": 0.40},
    "gpt-5.6-terra": {"input": 4.00, "output": 18.00, "cache_create": 5.00, "cache_read": 0.40},
    "gpt-6-luna": {"input": 0.20, "output": 0.75, "cache_create": 0.25, "cache_read": 0.02},
}
LONG_CONTEXT_INPUT_TOKENS = 272_000
FALLBACK_PRICE = PRICE_PER_MILLION_TOKENS_USD["claude-opus-5"]

TIERED_PRICE_PER_MILLION_TOKENS_USD = {
    "deepseek-flash": {
        "peak": {"input": 0.30, "output": 1.20, "cache_create": 0.30, "cache_read": 0.006},
        "off_peak": {"input": 0.15, "output": 0.60, "cache_create": 0.15, "cache_read": 0.003},
    },
    "deepseek-v4.1-flash": {
        "peak": {"input": 0.30, "output": 1.20, "cache_create": 0.30, "cache_read": 0.006},
        "off_peak": {"input": 0.15, "output": 0.60, "cache_create": 0.15, "cache_read": 0.003},
    },
}
PEAK_HOURS_UTC = ((1, 4), (6, 10))

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

# Do not backfill old conversations into the current project's usage log.
CATCHUP_SECONDS = 60
MOMENTS_KEPT = 200
LOGGED_IDS_KEPT = 2000

TRANSCRIPT_SUFFIX = ".jsonl"
META_SUFFIX = ".meta.json"
SUBAGENTS_DIRECTORY = "subagents"

LOG_FILENAME = "ai-requests.log"
STATE_FILENAME = ".ai-usage-state.json"
LOCK_FILENAME = ".ai-usage.lock"

STALE_TRANSCRIPT_SECONDS = 6 * 60 * 60
TOKENS_PER_MILLION = 1_000_000

SYNTHETIC_MODEL = "<synthetic>"
TRANSCRIPT_ID_LENGTH = 12
REPLY_ID_LENGTH = 8
STORED_MONEY_DECIMALS = 4
LOGGED_COST_DECIMALS = 2
LOGGED_BREAKDOWN_DECIMALS = 4
FIELD_SEPARATOR = " | "
CONVERSATION_FIELD = "conv="
ID_FIELD = "id="

COMMAND_NAME_OPEN = "<command-name>"
COMMAND_NAME_CLOSE = "</command-name>"


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
        and bool(message.get("id"))
        and message.get("model") != SYNTHETIC_MODEL
    )


def epoch_of(timestamp):
    try:
        return datetime.datetime.fromisoformat(timestamp.replace("Z", "+00:00")).timestamp()
    except (AttributeError, ValueError):
        return 0


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


def conversation_name(entry, transcript_path, session_name):
    own = entry["title"] or entry["command"]
    if own:
        return without_field_separators(own)
    if not session_name:
        return placeholder_name(transcript_path)
    agent = agent_type_of(transcript_path)
    return without_field_separators(f"{session_name} ({agent})" if agent else session_name)


def session_transcripts(transcript_path):
    folder = os.path.dirname(transcript_path)
    parent = os.path.dirname(folder) + TRANSCRIPT_SUFFIX
    root = parent if os.path.basename(folder) == SUBAGENTS_DIRECTORY else transcript_path
    subagents = glob.glob(os.path.join(root[: -len(TRANSCRIPT_SUFFIX)], SUBAGENTS_DIRECTORY, "*" + TRANSCRIPT_SUFFIX))
    return [root, *sorted(subagents)]


def flat_price_of(model):
    for known_model in sorted(PRICE_PER_MILLION_TOKENS_USD, key=len, reverse=True):
        if known_model in model:
            return PRICE_PER_MILLION_TOKENS_USD[known_model]
    return None


def is_peak_hour(spent_at):
    moment = datetime.datetime.fromtimestamp(spent_at, datetime.timezone.utc)
    if moment.weekday() > 4:
        return False
    return any(start <= moment.hour < end for start, end in PEAK_HOURS_UTC)


def tiered_price_of(model, spent_at):
    for known_model in sorted(TIERED_PRICE_PER_MILLION_TOKENS_USD, key=len, reverse=True):
        if known_model in model:
            rates = TIERED_PRICE_PER_MILLION_TOKENS_USD[known_model]
            return rates["peak"] if is_peak_hour(spent_at) else rates["off_peak"]
    return None


def price_of(model, spent_at):
    return tiered_price_of(model, spent_at) or flat_price_of(model) or FALLBACK_PRICE


def token_counts(usage):
    return {kind: usage.get(field, 0) for kind, field in USAGE_FIELD.items()}


def costs_in_usd(model, tokens, spent_at):
    price = price_of(model, spent_at)
    if sum(tokens[kind] for kind in ("input", "cache_create", "cache_read")) > LONG_CONTEXT_INPUT_TOKENS:
        for known_model, long_context_price in LONG_CONTEXT_PRICE_PER_MILLION_TOKENS_USD.items():
            if known_model in model:
                price = long_context_price
                break
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
        "last_reply_id": "",
        "moments": {},
        "logged_ids": [],
        "cumulative_thb": 0.0,
        "cumulative_usd": 0.0,
        "cumulative_output": 0,
    }


def entry_for(transcripts, transcript_path):
    stored = transcripts.get(transcript_path, {})
    entry = {key: stored.get(key, default) for key, default in empty_entry().items()}
    if not entry["last_reply_id"] and stored.get("spend"):
        entry["last_reply_id"] = next(reversed(stored["spend"]))
    return entry


def remember(entry, chunk):
    entry["title"] = chunk["title"] or entry["title"]
    entry["command"] = entry["command"] or chunk["command"]
    return {**entry["moments"], **chunk["moments"]}


def entries_for_new_replies(entry, chunk, name, log_cutoff, moments):
    logged = []
    for record in chunk["replies"]:
        message = record["message"]
        reply_id = message["id"]
        if reply_id == entry["last_reply_id"]:
            continue
        entry["last_reply_id"] = reply_id

        tokens = token_counts(message["usage"])
        spent_at = epoch_of(record.get("timestamp", ""))
        if spent_at < log_cutoff:
            continue

        costs = costs_in_usd(message.get("model", "unknown"), tokens, spent_at)
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
    state["transcripts"] = {
        path: entry for path, entry in transcripts.items()
        if now - entry["seen_at"] < STALE_TRANSCRIPT_SECONDS
    }


def main():
    event = read_event()
    if event.get("stop_hook_active"):
        return 0

    transcript_path = os.path.expanduser(event.get("transcript_path", ""))
    if not os.path.isfile(transcript_path) or is_codex_rollout(transcript_path):
        return 0

    if event.get("hook_event_name") != PRE_TOOL_USE:
        wait_until_settled(transcript_path)

    logs_dir = logs_directory(event)
    os.makedirs(logs_dir, exist_ok=True)
    log_file = os.path.join(logs_dir, LOG_FILENAME)
    state_file = os.path.join(logs_dir, STATE_FILENAME)
    now = time.time()

    with exclusive_lock(os.path.join(logs_dir, LOCK_FILENAME)):
        state = load_state(state_file)
        state.pop("notified_at", None)
        account_for(state, transcript_path, log_file, now)
        save_state(state_file, state)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as failure:  # Logging failures must never stop Claude Code.
        print(f"[ai-usage] skipped, {failure}", file=sys.stderr)
        sys.exit(0)
