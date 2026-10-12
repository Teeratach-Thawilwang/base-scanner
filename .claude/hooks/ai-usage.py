#!/usr/bin/env python3
import datetime
import glob
import importlib.util
import json
import math
import os
import sys
import time

from hook_lib import FILE_ENCODING, exclusive_lock, load_state, logs_directory, read_event, save_state

USD_TO_THB = 34

price_spec = importlib.util.spec_from_file_location(
    "usage_price_lookup", os.path.join(os.path.dirname(__file__), "ai-model-price.py")
)
price_lookup = importlib.util.module_from_spec(price_spec)
price_spec.loader.exec_module(price_lookup)

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


def parsed_timestamp(text):
    try:
        return datetime.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        return None


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


def price_of(model, spent_at):
    return price_lookup.selected_rates(model, spent_at)


def token_counts(usage):
    return {kind: usage.get(field, 0) for kind, field in USAGE_FIELD.items()}


def costs_in_usd(model, tokens, spent_at):
    context = sum(tokens[kind] for kind in ("input", "cache_create", "cache_read"))
    price = price_lookup.selected_rates(model, spent_at, context)
    try:
        costs = {kind: count * price[kind] / TOKENS_PER_MILLION for kind, count in tokens.items()}
        if any(not math.isfinite(cost * USD_TO_THB) or cost < 0 for cost in costs.values()):
            raise ValueError("invalid cost")
    except (ValueError, OverflowError):
        raise price_lookup.PriceLookupError("cost_invalid") from None
    return costs


def converted_to(currency_rate, costs):
    return {kind: round(cost * currency_rate, STORED_MONEY_DECIMALS) for kind, cost in costs.items()}


def total(costs):
    return round(sum(costs.values()), STORED_MONEY_DECIMALS)


def money(thb, usd, decimals):
    return f"[{thb:.{decimals}f}฿, ${usd:.{decimals}f}]"


def reply_mode(message):
    blocks = message.get("content", [])
    thinking = any(isinstance(block, dict) and block.get("type") == "thinking" for block in blocks)
    return "thinking" if thinking else "standard"


def elapsed_between(started, completed):
    if not started or not completed:
        return "N/A"
    return f"{(completed - started).total_seconds():.1f}s"


def format_entry(record, conversation, tokens, thb, usd, totals, elapsed, reason=""):
    def cost_field(kind):
        return "null" if usd is None else money(thb[kind], usd[kind], LOGGED_BREAKDOWN_DECIMALS)

    message = record["message"]
    return FIELD_SEPARATOR.join(
        [
            f"model={message.get('model', 'unknown')}",
            f"cost={'null' if usd is None else money(total(thb), total(usd), LOGGED_COST_DECIMALS)}",
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
            f"cost_input={cost_field('input')}",
            f"cost_output={cost_field('output')}",
            f"cost_cache_create={cost_field('cache_create')}",
            f"cost_cache_read={cost_field('cache_read')}",
            f"duration={elapsed}",
            f"{ID_FIELD}{record.get('uuid', '')[:REPLY_ID_LENGTH]}",
            f"cumulative_unknown={totals['cumulative_unknown']}",
            *([f"cost_reason={reason}"] if reason else []),
        ]
    )


def add_to_running_totals(totals, thb, usd, tokens):
    if usd is None:
        totals["cumulative_unknown"] += 1
    else:
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
        "cumulative_unknown": 0,
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
        completed = parsed_timestamp(record.get("timestamp"))
        spent_at = completed.timestamp() if completed else 0
        if spent_at < log_cutoff:
            continue

        reason = ""
        try:
            costs = costs_in_usd(message.get("model", "unknown"), tokens, spent_at)
            thb = converted_to(USD_TO_THB, costs)
            usd = converted_to(1, costs)
        except price_lookup.PriceLookupError as failure:
            reason = str(failure)
            thb = usd = None
        add_to_running_totals(entry, thb, usd, tokens)
        elapsed = elapsed_between(parsed_timestamp(moments.get(record.get("parentUuid"))), completed)
        entry["logged_ids"].append(record.get("uuid", "")[:REPLY_ID_LENGTH])
        logged.append((record.get("timestamp", ""), format_entry(record, name, tokens, thb, usd, entry, elapsed, reason)))
    if len(entry["logged_ids"]) > LOGGED_IDS_KEPT:
        entry["logged_ids"] = entry["logged_ids"][-LOGGED_IDS_KEPT:]
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
    wanted = set(ids)
    lines = [renamed(line, wanted, name) for line in logged]
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
    progressed = offset != entry["offset"]
    moments = remember(entry, chunk)
    entry["offset"] = offset
    entry["seen_at"] = now

    name = conversation_name(entry, transcript_path, session_name)
    logged = entries_for_new_replies(entry, chunk, name, now - CATCHUP_SECONDS if first_sight else 0, moments)
    entry["moments"] = trimmed(moments, MOMENTS_KEPT)
    renamed_conversation = name != entry["name"]
    if renamed_conversation:
        rename_logged_replies(log_file, entry["logged_ids"], name)
        entry["name"] = name
    append_lines(log_file, [line for _, line in sorted(logged, key=lambda logged_reply: logged_reply[0])])

    transcripts[transcript_path] = entry
    return name, first_sight or progressed or renamed_conversation or bool(logged)


def account_for(state, transcript_path, log_file, now):
    transcripts = state.setdefault("transcripts", {})
    session_name = ""
    absorbed = False
    for path in session_transcripts(transcript_path):
        if os.path.isfile(path):
            name, changed = absorb(transcripts, path, log_file, now, session_name)
            absorbed = absorbed or changed
            session_name = session_name or name
    kept = {
        path: entry for path, entry in transcripts.items()
        if now - entry["seen_at"] < STALE_TRANSCRIPT_SECONDS
    }
    state["transcripts"] = kept
    return absorbed or len(kept) != len(transcripts)


def main():
    price_lookup.reset_cache()
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
        retired_notice = state.pop("notified_at", None) is not None
        changed = account_for(state, transcript_path, log_file, now)
        if changed or retired_notice:
            save_state(state_file, state)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as failure:  # Logging failures must never stop Claude Code.
        print(f"[ai-usage] skipped, {failure}", file=sys.stderr)
        sys.exit(0)
