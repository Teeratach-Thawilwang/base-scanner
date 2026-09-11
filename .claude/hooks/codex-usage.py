#!/usr/bin/env python3
# Stop and PreToolUse hook: write down every Codex API request as it happens.
#
# The twin of ai-usage.py, and deliberately not the same file. That one prices Anthropic
# tokens off a Claude transcript; this one reads a Codex rollout, where the same numbers
# live in other records under other names.
#
# The money in this log is an estimate and nothing else. A ChatGPT plan is not billed per
# token, so the cost column is what these tokens would have cost through the API, priced
# off the table below. The plan's real price is the quota column beside it, which is the
# share of the window the request actually ate and is measured, not computed.
#
# What the rollout gives, and where:
#   token_usage_record   one per API request. `usage` is that request, `thread_token_usage`
#                        the session so far, and `response_id` is what makes two copies of
#                        a record one request
#   turn_context         the model actually serving the turn, which token_usage_record
#                        never repeats
#   event_msg/token_count  rate_limits.primary.used_percent, the only honest answer to
#                        "how much did this cost", and it moves in whole percents
#   event_msg/user_message the prompt, which is the closest thing a rollout has to a title
#
# The rollout is append-only, so each pass reads the bytes added since the last one.

import datetime
import glob
import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from hook_lib import FILE_ENCODING, exclusive_lock, load_state, read_event, save_state

# --- settings, edit these -------------------------------------------------
LOG_DIRECTORY = (".codex", "logs")  # under the project root
NAME_LENGTH = 60
USD_TO_THB = 34

# Measured over every Codex rollout on this machine, which is 52 requests across 4
# sessions. That is a thin sample next to the one behind ai-usage.py, so these sit at
# roughly twice the worst seen rather than just under it, and they are meant to be
# retightened once there is a month of real work to measure.
#   five minutes   requests worst 21   tokens worst 1.3M
#   one hour       requests worst 36   tokens worst 1.9M
FIVE_MINUTE_REQUEST_LIMIT = 50
FIVE_MINUTE_TOKEN_LIMIT = 15_000_000
HOURLY_REQUEST_LIMIT = 200
HOURLY_TOKEN_LIMIT = 50_000_000

# The other ceiling, and the only one that is not guesswork: the share of the plan's own
# window already spent, which the rollout is told by the server on every request.
QUOTA_CEILING_PERCENT = 95.0

NOTIFY_ON_BLOCK = True
# `CODEX_RATE_LIMIT_OFF=1 codex` uncaps this runtime alone, because the two plans are
# metered differently and hitting the ceiling on one says nothing about the other.
# RATE_LIMIT_OFF, which ai-usage.py reads as well, still silences both at once.
OFF_SWITCH_VARIABLES = ("CODEX_RATE_LIMIT_OFF", "RATE_LIMIT_OFF")

# Keyed by the tier in the model name: gpt-5.6-terra is terra. Only input and output are
# published, so the cached rate is the usual tenth of input rather than a figure of its
# own, and it is the one number in this file that is a convention and not a quote.
PRICE_PER_MILLION_TOKENS_USD = {
    "astra": {"input": 10.00, "output": 50.00},
    "sol": {"input": 4.00, "output": 20.00},
    "terra": {"input": 2.00, "output": 12.00},
    "luna": {"input": 0.20, "output": 1.20},
}
CACHED_INPUT_SHARE = 0.10
# A model off the table, gpt-5.3-codex among them, is priced at the dearest tier so the
# estimate leans high rather than low, and its cost is written with a ~ to say so.
FALLBACK_TIER = "astra"
# --------------------------------------------------------------------------

REQUESTS = "requests"
TOKENS = "tokens"

# window in minutes, what is counted, how much of it is allowed
BUDGETS = (
    (5, REQUESTS, FIVE_MINUTE_REQUEST_LIMIT),
    (5, TOKENS, FIVE_MINUTE_TOKEN_LIMIT),
    (60, REQUESTS, HOURLY_REQUEST_LIMIT),
    (60, TOKENS, HOURLY_TOKEN_LIMIT),
)

PRE_TOOL_USE = "PreToolUse"
SECONDS_PER_MINUTE = 60
NOTIFY_SCRIPT = "notify-done.sh"
NOTIFY_TITLE = "Codex hit the API cap"
NOTIFY_COOLDOWN_SECONDS = 120
NOTIFY_TIMEOUT_SECONDS = 5
BLOCKED_EVENT = "event=rate-limit-block"

BLOCK_INSTRUCTION = (
    "[rate-limit] BLOCKED: {spent} {metric} in the last {minutes} minutes, the cap is {limit}.\n"
    "Stop here. Do not retry this call and do not reach for a different tool — every retry "
    "is one more request against the same cap.\n"
    "End the turn now with one line saying what you were doing and what is left. "
    "The user has been notified and decides what happens next."
)
QUOTA_INSTRUCTION = (
    "[rate-limit] BLOCKED: {spent:.1f}% of the {window} plan window is already spent, the "
    "cap is {limit:.1f}%.\n"
    "Stop here. Do not retry this call and do not reach for a different tool.\n"
    "End the turn now with one line saying what you were doing and what is left. "
    "The user has been notified and decides what happens next."
)

LOG_FILENAME = "ai-requests.log"
STATE_FILENAME = ".codex-usage-state.json"
LOCK_FILENAME = ".codex-usage.lock"

USAGE_RECORD = "token_usage_record"
TURN_CONTEXT = "turn_context"
EVENT_MESSAGE = "event_msg"
TOKEN_COUNT = "token_count"
USER_MESSAGE = "user_message"
SESSION_META = "session_meta"

RESPONSE_ID_LENGTH = 10
RESPONSE_ID_PREFIX = "resp_"
SESSION_ID_LENGTH = 8
MINUTES_PER_DAY = 1440
TOKENS_PER_MILLION = 1_000_000
LOGGED_COST_DECIMALS = 2
STALE_ROLLOUT_SECONDS = 6 * 60 * 60
LOGGED_IDS_KEPT = 4000
FIELD_SEPARATOR = " | "

USAGE_FIELDS = (
    ("input", "input_tokens"),
    ("cached", "cached_input_tokens"),
    ("cache_write", "cache_write_input_tokens"),
    ("output", "output_tokens"),
    ("reasoning", "reasoning_output_tokens"),
)


# transcript_path is documented as nullable. When it is missing the rollout is still on
# disk under the session id, so it is found rather than the hook quietly logging nothing.
def rollout_for(event):
    named = os.path.expanduser(event.get("transcript_path", "") or "")
    if named and os.path.isfile(named):
        return named
    session_id = event.get("session_id", "")
    if not session_id:
        return ""
    home = os.environ.get("CODEX_HOME") or os.path.join(os.path.expanduser("~"), ".codex")
    found = glob.glob(os.path.join(home, "sessions", "*", "*", "*", "rollout-*-%s.jsonl" % session_id))
    return found[0] if found else ""


# cwd is where the session was started, which is not the repo root when someone opens
# Codex inside studio/. The log belongs to the project, not to the folder a session
# happened to begin in, so the root is asked of git and cwd is only the fallback.
def project_root(event):
    named = os.environ.get("CLAUDE_PROJECT_DIR")
    if named:
        return named
    cwd = event.get("cwd") or os.getcwd()
    try:
        found = subprocess.run(
            ["git", "-C", cwd, "rev-parse", "--show-toplevel"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
        root = found.stdout.decode(FILE_ENCODING, "replace").strip()
        if found.returncode == 0 and root:
            return root
    except (OSError, subprocess.SubprocessError):
        pass
    return cwd


def logs_directory(event):
    return os.path.join(project_root(event), *LOG_DIRECTORY)


def epoch_of(timestamp):
    try:
        return datetime.datetime.fromisoformat(timestamp.replace("Z", "+00:00")).timestamp()
    except (AttributeError, ValueError):
        return 0.0


def iso_of(epoch):
    if not epoch:
        return ""
    stamped = datetime.datetime.fromtimestamp(epoch, datetime.timezone.utc)
    return stamped.isoformat(timespec="seconds").replace("+00:00", "Z")


def unread_lines(rollout_path, offset):
    with open(rollout_path, "rb") as rollout:
        size = rollout.seek(0, os.SEEK_END)
        start = 0 if offset > size else offset
        rollout.seek(start)
        unread = rollout.read()
    last_break = unread.rfind(b"\n")
    if last_break < 0:
        return [], start
    complete = unread[: last_break + 1]
    return complete.decode(FILE_ENCODING, "replace").splitlines(), start + len(complete)


def without_field_separators(name):
    return " ".join(name.replace("|", "/").split())


# A rollout opens with the plugin catalogue and other harness text before the person's
# own words, so the first user_message event is taken and not the first response_item.
def shortened(prompt):
    lines = [line.strip() for line in prompt.split("\n") if line.strip()]
    if not lines:
        return ""
    name = without_field_separators(lines[0])
    return name[: NAME_LENGTH - 3] + "..." if len(name) > NAME_LENGTH else name


def window_label(minutes):
    if not minutes:
        return "?"
    if minutes % MINUTES_PER_DAY == 0:
        return "%dd" % (minutes // MINUTES_PER_DAY)
    return "%dh" % (minutes // 60)


def quota_label(quota):
    if not quota:
        return "quota=unknown"
    return "quota=%.1f%% (%s, resets %s)" % (
        quota.get("used_percent", 0.0),
        window_label(quota.get("window_minutes", 0)),
        iso_of(quota.get("resets_at", 0)) or "?",
    )


# gpt-5.6-terra -> terra. The tier is a whole dash-separated part, never a substring, or
# "sol" would match the middle of a name that has nothing to do with that tier.
def price_of(model):
    for part in str(model).lower().split("-"):
        if part in PRICE_PER_MILLION_TOKENS_USD:
            return PRICE_PER_MILLION_TOKENS_USD[part], True
    return PRICE_PER_MILLION_TOKENS_USD[FALLBACK_TIER], False


# input_tokens already contains cached_input_tokens, so the uncached part is the
# difference. cache_write is not billed separately and is left inside that difference.
def cost_in_usd(model, usage):
    price, known = price_of(model)
    cached = usage.get("cached_input_tokens", 0) or 0
    uncached = max((usage.get("input_tokens", 0) or 0) - cached, 0)
    output = usage.get("output_tokens", 0) or 0
    usd = (
        uncached * price["input"]
        + cached * price["input"] * CACHED_INPUT_SHARE
        + output * price["output"]
    ) / TOKENS_PER_MILLION
    return usd, known


def money(usd, known):
    return "%s[%.*f฿, $%.*f]" % (
        "" if known else "~",
        LOGGED_COST_DECIMALS,
        usd * USD_TO_THB,
        LOGGED_COST_DECIMALS,
        usd,
    )


# Every response_id opens with resp_ and the same long run of shared characters, so the
# head of one is the head of all of them. What tells two apart is the tail.
def short_id(response_id):
    body = response_id[len(RESPONSE_ID_PREFIX):] if response_id.startswith(RESPONSE_ID_PREFIX) else response_id
    return body[-RESPONSE_ID_LENGTH:]


def tokens_of(usage):
    return {name: usage.get(field, 0) or 0 for name, field in USAGE_FIELDS}


def read_chunk(lines, entry):
    """Fold the new bytes into: the requests found, plus whatever context they need."""
    chunk = {"requests": [], "counted": [], "model": entry["model"], "quota": entry["quota"], "name": entry["name"]}
    turn_started = dict(entry["turn_started"])

    for line in lines:
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if not isinstance(record, dict):
            continue

        kind = record.get("type")
        payload = record.get("payload")
        if not isinstance(payload, dict):
            continue
        stamp = record.get("timestamp", "")

        if kind == TURN_CONTEXT:
            chunk["model"] = payload.get("model") or chunk["model"]
            turn_started.setdefault(payload.get("turn_id", ""), stamp)
            continue

        if kind == EVENT_MESSAGE and payload.get("type") == TOKEN_COUNT:
            primary = ((payload.get("rate_limits") or {}).get("primary")) or {}
            if primary:
                chunk["quota"] = primary
            info = payload.get("info") or {}
            last = info.get("last_token_usage") or {}
            if last:
                chunk["counted"].append((stamp, last, info.get("total_token_usage") or {}))
            continue

        if kind == EVENT_MESSAGE and payload.get("type") == USER_MESSAGE:
            if not chunk["name"]:
                chunk["name"] = shortened(payload.get("message", "") or "")
            continue

        if kind == USAGE_RECORD and payload.get("response_id"):
            chunk["requests"].append((stamp, payload))

    chunk["turn_started"] = turn_started
    return chunk


def elapsed_for(payload, stamp, entry, chunk):
    turn_id = payload.get("turn_id", "")
    previous = entry["turn_last_seen"].get(turn_id) or chunk["turn_started"].get(turn_id, "")
    entry["turn_last_seen"][turn_id] = stamp
    if not previous:
        return "N/A"
    seconds = epoch_of(stamp) - epoch_of(previous)
    return "%.1fs" % seconds if seconds >= 0 else "N/A"


# cost_total is this session and nothing else, the same as in ai-usage.py, because a
# number that mixed every session together answered no question anyone asks of a log.
# It is priced off the thread totals the rollout already carries rather than added up
# here, so a state file pruned between two requests cannot make it drift.
def format_entry(payload, stamp, model, quota, name, tokens, project_total, cost, elapsed):
    usd, known = cost
    thread = payload.get("thread_token_usage") or {}
    session_usd, _ = cost_in_usd(model, thread)
    return FIELD_SEPARATOR.join(
        [
            "model=%s" % (model or "unknown"),
            "cost=%s" % money(usd, known),
            "cost_total=%s" % money(session_usd, known),
            "conv=%s" % (name or "untitled"),
            quota_label(quota),
            stamp,
            *("%s=%d" % (label, count) for label, count in tokens.items()),
            "total=%d" % ((payload.get("usage") or {}).get("total_tokens", 0)),
            "session_total=%d" % (thread.get("total_tokens", 0)),
            "project_total=%d" % project_total,
            "duration=%s" % elapsed,
            "id=%s" % short_id(payload["response_id"]),
        ]
    )


def empty_entry():
    return {
        "offset": 0,
        "seen_at": 0,
        "model": "",
        "name": "",
        "quota": {},
        "turn_started": {},
        "turn_last_seen": {},
        "logged_ids": [],
        "uses_records": False,
        "spend": {},  # one request is [epoch seconds, tokens], keyed by its response_id
    }


def entry_for(rollouts, rollout_path):
    stored = rollouts.get(rollout_path, {})
    return {key: stored.get(key, default) for key, default in empty_entry().items()}


def append_lines(log_file, lines):
    if not lines:
        return
    with open(log_file, "a", encoding=FILE_ENCODING) as log:
        for line in lines:
            log.write(line + "\n")


def longest_budget_seconds():
    return max(minutes for minutes, _, _ in BUDGETS) * SECONDS_PER_MINUTE


def still_in_budget(spend, oldest_kept):
    return {
        response_id: seen
        for response_id, seen in spend.items()
        if isinstance(seen, list) and len(seen) == 2 and seen[0] >= oldest_kept
    }


# A rollout is dropped once it has gone quiet and has nothing left inside any window.
# Its byte offset goes with it, so it is only ever dropped when it can no longer be read
# again without double counting.
def pruned(rollouts, now):
    oldest_kept = now - longest_budget_seconds()
    kept = {}
    for path, entry in rollouts.items():
        entry["spend"] = still_in_budget(entry.get("spend", {}), oldest_kept)
        if entry["spend"] or now - entry.get("seen_at", 0) < STALE_ROLLOUT_SECONDS:
            kept[path] = entry
    return kept


def spent_since(rollouts, oldest_counted):
    spends = [
        seen
        for entry in rollouts.values()
        for seen in entry.get("spend", {}).values()
        if seen[0] >= oldest_counted
    ]
    return {REQUESTS: len(spends), TOKENS: sum(seen[1] for seen in spends)}


# Every window, as (minutes, metric, spent, limit), in the order BUDGETS lists them.
def spend_per_budget(rollouts, now):
    windows = {
        minutes: spent_since(rollouts, now - minutes * SECONDS_PER_MINUTE)
        for minutes, _, _ in BUDGETS
    }
    return [(minutes, metric, windows[minutes][metric], limit) for minutes, metric, limit in BUDGETS]


def first_breach(spend):
    for budget in spend:
        if budget[2] >= budget[3]:
            return budget
    return None


# The server's own number, off the newest rollout that carries one.
def quota_breach(rollouts):
    newest = None
    for entry in rollouts.values():
        quota = entry.get("quota") or {}
        if not quota:
            continue
        if newest is None or entry.get("seen_at", 0) > newest[0]:
            newest = (entry.get("seen_at", 0), quota)
    if newest is None:
        return None
    quota = newest[1]
    return quota if (quota.get("used_percent") or 0) >= QUOTA_CEILING_PERCENT else None


def cap_is_on():
    return not any(os.environ.get(name, "") == "1" for name in OFF_SWITCH_VARIABLES)


def due_for_notice(state, session_id, now):
    if not NOTIFY_ON_BLOCK:
        return False
    notified_at = state.setdefault("notified_at", {})
    if now - notified_at.get(session_id, 0) < NOTIFY_COOLDOWN_SECONDS:
        return False
    notified_at[session_id] = now
    return True


def notify(status):
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), NOTIFY_SCRIPT)
    try:
        subprocess.run(
            ["bash", script, NOTIFY_TITLE, status],
            stdin=subprocess.DEVNULL,
            timeout=NOTIFY_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        pass


def blocked_line(reason, spend, tool_name, session_id, now):
    return FIELD_SEPARATOR.join(
        [
            BLOCKED_EVENT,
            "breached=%s" % reason,
            *("%dm/%s=%d/%d" % (minutes, metric, spent, limit) for minutes, metric, spent, limit in spend),
            "tool=%s" % tool_name,
            "session=%s" % session_id[:SESSION_ID_LENGTH],
            iso_of(now),
        ]
    )


# Verified against codex 0.154: deny plus a non-empty reason is the only decision
# PreToolUse accepts, and continue/stopReason/suppressOutput are rejected outright, so
# none of them are sent. Exit 2 with the same reason on stderr is the other path Codex
# takes, and sending both is what the live test ran.
def deny(reason):
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": PRE_TOOL_USE,
                    "permissionDecision": "deny",
                    "permissionDecisionReason": reason,
                }
            }
        )
    )
    print(reason, file=sys.stderr)
    return 2


def readable(metric, amount):
    if metric != TOKENS:
        return "{:,}".format(amount)
    return "%.1fM" % (amount / TOKENS_PER_MILLION)


def absorb(state, rollout_path, log_file, now):
    rollouts = state.setdefault("rollouts", {})
    entry = entry_for(rollouts, rollout_path)

    lines, offset = unread_lines(rollout_path, entry["offset"])
    chunk = read_chunk(lines, entry)
    entry["offset"] = offset
    entry["seen_at"] = now
    entry["model"] = chunk["model"]
    entry["quota"] = chunk["quota"]
    entry["name"] = chunk["name"]

    # A rollout written by an older CLI has no token_usage_record at all, only the
    # token_count event, which says the same thing without a response_id. It is used only
    # when this rollout has never produced a real record, or the two would double count.
    entry["uses_records"] = entry["uses_records"] or bool(chunk["requests"])
    requests = chunk["requests"]
    if not entry["uses_records"]:
        requests = [
            (
                stamp,
                {
                    "response_id": "count_%s" % stamp,
                    "usage": last,
                    "thread_token_usage": running,
                    "turn_id": "",
                },
            )
            for stamp, last, running in chunk["counted"]
        ]

    already = set(entry["logged_ids"])
    logged = []
    for stamp, payload in requests:
        response_id = payload["response_id"]
        if response_id in already:
            continue
        already.add(response_id)
        entry["logged_ids"] = (entry["logged_ids"] + [response_id])[-LOGGED_IDS_KEPT:]

        usage = payload.get("usage") or {}
        entry["spend"][response_id] = [epoch_of(stamp), usage.get("total_tokens", 0) or 0]
        tokens = tokens_of(usage)
        usd, known = cost_in_usd(entry["model"], usage)

        state["project_total"] = state.get("project_total", 0) + (usage.get("total_tokens", 0) or 0)

        elapsed = elapsed_for(payload, stamp, entry, chunk)
        logged.append(
            (
                stamp,
                format_entry(
                    payload,
                    stamp,
                    entry["model"],
                    entry["quota"],
                    entry["name"],
                    tokens,
                    state["project_total"],
                    (usd, known),
                    elapsed,
                ),
            )
        )

    append_lines(log_file, [line for _, line in sorted(logged, key=lambda logged_reply: logged_reply[0])])
    rollouts[rollout_path] = entry
    state["rollouts"] = pruned(rollouts, now)


def main():
    event = read_event()

    rollout_path = rollout_for(event)
    if not rollout_path or not os.path.isfile(rollout_path):
        return 0

    logs_dir = logs_directory(event)
    os.makedirs(logs_dir, exist_ok=True)
    log_file = os.path.join(logs_dir, LOG_FILENAME)
    state_file = os.path.join(logs_dir, STATE_FILENAME)

    # PreToolUse is the only event that lands in the gap between two requests, so it is
    # the only one that can stop a run before the turn ends. Stop just closes the books.
    on_tool_call = event.get("hook_event_name") == PRE_TOOL_USE
    session_id = str(event.get("session_id", ""))
    now = time.time()

    with exclusive_lock(os.path.join(logs_dir, LOCK_FILENAME)):
        state = load_state(state_file)
        absorb(state, rollout_path, log_file, now)
        rollouts = state.get("rollouts", {})
        spend = spend_per_budget(rollouts, now)

        reason = ""
        if on_tool_call and cap_is_on():
            budget = first_breach(spend)
            if budget:
                minutes, metric, spent, limit = budget
                reason = BLOCK_INSTRUCTION.format(
                    spent=readable(metric, spent),
                    metric=metric,
                    minutes=minutes,
                    limit=readable(metric, limit),
                )
                breached = "%dm/%s" % (minutes, metric)
            else:
                quota = quota_breach(rollouts)
                if quota:
                    reason = QUOTA_INSTRUCTION.format(
                        spent=quota.get("used_percent", 0.0),
                        window=window_label(quota.get("window_minutes", 0)),
                        limit=QUOTA_CEILING_PERCENT,
                    )
                    breached = "quota/%s" % window_label(quota.get("window_minutes", 0))

        announce = bool(reason) and due_for_notice(state, session_id, now)
        if reason:
            append_lines(log_file, [blocked_line(breached, spend, str(event.get("tool_name", "")), session_id, now)])
        save_state(state_file, state)

    if not reason:
        return 0
    if announce:
        notify(reason.split("\n")[0].replace("[rate-limit] BLOCKED: ", "") + " Tool calls blocked.")
    return deny(reason)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as failure:  # a broken accountant must never be the thing that wedges a session
        print("[codex-usage] skipped, %s" % failure, file=sys.stderr)
        sys.exit(0)
