"""Portable, bounded offline price lookup through the adapter's public command."""

import datetime
import json
import math
import os
import re
import selectors
import subprocess
import time
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

MAX_OUTPUT_BYTES = 64 * 1024
LOOKUP_TIMEOUT_SECONDS = 2.0
RATES = {"input", "output", "cache_create", "cache_read"}
_cache = {}
_lookup_seconds_remaining = 3.0


class PriceLookupError(ValueError):
    pass


def reset_cache():
    global _lookup_seconds_remaining
    _cache.clear()
    _lookup_seconds_remaining = 3.0


def bounded_output(arguments, budget_seconds):
    try:
        process = subprocess.Popen(arguments, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    except FileNotFoundError:
        raise PriceLookupError("lookup_unavailable") from None
    except OSError:
        raise PriceLookupError("lookup_failed") from None
    deadline = time.monotonic() + min(LOOKUP_TIMEOUT_SECONDS, budget_seconds)
    output = bytearray()
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not selector.select(remaining):
                    raise PriceLookupError("lookup_timeout")
                chunk = os.read(process.stdout.fileno(), min(4096, MAX_OUTPUT_BYTES + 1 - len(output)))
                if not chunk:
                    break
                output.extend(chunk)
                if len(output) > MAX_OUTPUT_BYTES:
                    raise PriceLookupError("lookup_oversize")
        try:
            status = process.wait(timeout=max(0.001, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            raise PriceLookupError("lookup_timeout") from None
        if status:
            raise PriceLookupError("lookup_failed")
        return bytes(output)
    finally:
        if process.poll() is None:
            process.kill()
        process.wait()
        process.stdout.close()


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def reject_constant(value):
    raise ValueError("nonfinite constant")


def require_keys(value, keys):
    if not isinstance(value, dict) or set(value) != keys:
        raise ValueError("invalid keys")


def validate_rates(rates, complete=False):
    if (not isinstance(rates, dict) or not rates or not set(rates) <= RATES
            or complete and set(rates) != RATES):
        raise ValueError("invalid rates")
    for value in rates.values():
        if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
            raise ValueError("invalid rate")


def minutes(value):
    if not isinstance(value, str) or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value):
        raise ValueError("invalid schedule time")
    hour, minute = map(int, value.split(":"))
    return hour * 60 + minute


def validate_price(price):
    require_keys(price, RATES | {"unit", "schedule", "tiers"})
    if price["unit"] != "usd_per_million_tokens":
        raise ValueError("invalid unit")
    validate_rates({key: price[key] for key in RATES}, complete=True)
    if not isinstance(price["tiers"], list) or not isinstance(price["schedule"], list):
        raise ValueError("invalid rules")
    previous = -1
    for tier in price["tiers"]:
        require_keys(tier, {"input_tokens_gt", "price"})
        threshold = tier["input_tokens_gt"]
        if type(threshold) is not int or threshold < 0 or threshold <= previous:
            raise ValueError("invalid threshold")
        previous = threshold
        validate_rates(tier["price"])
    windows = []
    for rule in price["schedule"]:
        require_keys(rule, {"timezone", "days", "start", "end", "price"})
        if not isinstance(rule["timezone"], str):
            raise ValueError("invalid timezone")
        ZoneInfo(rule["timezone"])
        days = rule["days"]
        if (not isinstance(days, list) or not days
                or any(type(day) is not int or not 0 <= day <= 6 for day in days)
                or len(days) != len(set(days))):
            raise ValueError("invalid days")
        start, end = minutes(rule["start"]), minutes(rule["end"]) or 1440
        if start >= end:
            raise ValueError("invalid window")
        validate_rates(rule["price"])
        for zone, old_days, old_start, old_end in windows:
            if zone != rule["timezone"] or set(days) & old_days and start < old_end and old_start < end:
                raise ValueError("unproven schedule overlap")
        windows.append((rule["timezone"], set(days), start, end))


def lookup_price(model, spent_at):
    if not isinstance(model, str) or not model or "\x00" in model:
        raise PriceLookupError("invalid_model")
    try:
        moment = datetime.datetime.fromtimestamp(spent_at, datetime.timezone.utc)
        timestamp = moment.isoformat().replace("+00:00", "Z")
    except (ValueError, OverflowError, TypeError, OSError):
        raise PriceLookupError("invalid_timestamp") from None
    key = (model, timestamp)
    if key in _cache:
        return _cache[key], moment
    global _lookup_seconds_remaining
    if _lookup_seconds_remaining <= 0:
        raise PriceLookupError("lookup_budget_exhausted")
    started = time.monotonic()
    try:
        output = bounded_output(["cc-model-price", model, "--at", timestamp], _lookup_seconds_remaining)
    finally:
        _lookup_seconds_remaining -= time.monotonic() - started
    try:
        response = json.loads(output, object_pairs_hook=unique_object, parse_constant=reject_constant)
        require_keys(response, {"model", "provider", "price"})
        if any(not isinstance(response[key], str) or not response[key] for key in ("model", "provider")):
            raise ValueError("invalid identity")
        validate_price(response["price"])
    except (ValueError, TypeError, OverflowError, UnicodeError, ZoneInfoNotFoundError):
        raise PriceLookupError("lookup_invalid") from None
    _cache[key] = response["price"]
    return response["price"], moment


def selected_rates(model, spent_at, input_tokens=0):
    price, moment = lookup_price(model, spent_at)
    selected = {key: price[key] for key in RATES}
    for rule in price["schedule"]:
        local = moment.astimezone(ZoneInfo(rule["timezone"]))
        if (local.weekday() in rule["days"]
                and minutes(rule["start"]) <= local.hour * 60 + local.minute < (minutes(rule["end"]) or 1440)):
            selected.update(rule["price"])
    for tier in price["tiers"]:
        if input_tokens > tier["input_tokens_gt"]:
            selected.update(tier["price"])
    return selected
