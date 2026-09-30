#!/usr/bin/env python3
"""Auto-approve a narrow read-only subset in plan mode only.

Maintenance notes for agents debugging or extending this hook:
- Only PreToolUse events with permission_mode == "plan" are eligible.
- Read, Glob, and Grep are eligible for approval. Bash must match the
  conservative read-only command and option allowlists below.
- Unknown commands, unsafe inputs, and other permission modes produce no
  decision, leaving Claude Code's normal permission checks in control.
  This hook does not grant approval for writes during planning.
- If readable settings contain an explicit ask or deny rule for the tool,
  defer to native permission evaluation. Do not bypass user restrictions.
- Never emit deny or ask, execute the requested command, or broaden approval
  just because a command appears read-only. Shell syntax, executable options,
  and resolved paths must all pass validation.
"""

import json
import os
import re
import shlex
import sys
from pathlib import Path


PRE_TOOL_USE = "PreToolUse"
PLAN_MODE = "plan"
AUTO_APPROVED_TOOLS = frozenset({"Read", "Glob", "Grep"})
SHELL_SYNTAX_CHARACTERS = frozenset(";|&<>`$(){}[]*?!#~\\\n\r\x00")
ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
COUNT = re.compile(r"^[0-9]+$")

LS_SHORT_FLAGS = frozenset("aAlh1d")
LS_LONG_FLAGS = frozenset(
    {"--all", "--almost-all", "--long", "--human-readable", "--directory"}
)
CAT_SHORT_FLAGS = frozenset("nbsETv")
HEAD_TAIL_SHORT_FLAGS = frozenset("qv")
WC_SHORT_FLAGS = frozenset("cmlLw")
SEARCH_SHORT_FLAGS = frozenset("inHhlLcowxFEGPqs")
SEARCH_LONG_FLAGS = frozenset(
    {
        "--ignore-case",
        "--line-number",
        "--with-filename",
        "--no-filename",
        "--files-with-matches",
        "--files-without-match",
        "--count",
        "--only-matching",
        "--word-regexp",
        "--line-regexp",
        "--fixed-strings",
        "--extended-regexp",
        "--basic-regexp",
        "--perl-regexp",
        "--no-messages",
        "--quiet",
        "--no-config",
    }
)
GIT_FLAGS = {
    "diff": frozenset(
        {
            "--cached",
            "--staged",
            "--stat",
            "--numstat",
            "--shortstat",
            "--name-only",
            "--name-status",
            "--summary",
            "--patch",
            "--no-patch",
            "--no-ext-diff",
            "--no-textconv",
        }
    ),
    "log": frozenset(
        {
            "--oneline",
            "--stat",
            "--name-only",
            "--name-status",
            "--no-decorate",
            "--decorate",
            "--no-ext-diff",
            "--no-textconv",
        }
    ),
    "show": frozenset(
        {
            "--stat",
            "--name-only",
            "--name-status",
            "--summary",
            "--no-patch",
            "--no-ext-diff",
            "--no-textconv",
        }
    ),
}
GIT_REQUIRED_SAFETY_FLAGS = frozenset({"--no-ext-diff", "--no-textconv"})
GIT_ENVIRONMENT_VARIABLES = frozenset(
    {
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    }
)


def has_shell_syntax(command):
    return any(character in command for character in SHELL_SYNTAX_CHARACTERS)


def command_words(command):
    if not isinstance(command, str) or not command.strip() or has_shell_syntax(command):
        return None
    try:
        words = shlex.split(command, posix=True)
    except ValueError:
        return None
    if not words or ASSIGNMENT.match(words[0]):
        return None
    return words


def is_allowed_short_flag_group(word, allowed_flags):
    return (
        word.startswith("-")
        and word != "-"
        and not word.startswith("--")
        and all(flag in allowed_flags for flag in word[1:])
    )


def directories_for(event):
    cwd = event.get("cwd")
    if not isinstance(cwd, str):
        return None
    cwd_path = Path(cwd)
    if not cwd_path.is_absolute():
        return None
    try:
        resolved_cwd = cwd_path.resolve(strict=True)
    except (OSError, RuntimeError, ValueError):
        return None
    if not resolved_cwd.is_dir():
        return None

    roots = {resolved_cwd}
    for parent in (resolved_cwd, *resolved_cwd.parents):
        if (parent / ".git").exists():
            roots.add(parent)
            break
    return resolved_cwd, tuple(roots)


def is_within(path, root):
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def is_safe_path(word, cwd, roots, allow_directories):
    if not word or word == "-":
        return False
    candidate = Path(word)
    if not candidate.is_absolute():
        candidate = cwd / candidate
    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError, ValueError):
        return False
    if not any(is_within(resolved, root) for root in roots):
        return False
    return resolved.is_file() or (allow_directories and resolved.is_dir())


def paths_are_safe(words, cwd, roots, allow_directories, require_path=True):
    return (
        bool(words) or not require_path
    ) and all(is_safe_path(word, cwd, roots, allow_directories) for word in words)


def options_and_paths(words, short_flags, long_flags):
    index = 0
    while index < len(words):
        word = words[index]
        if word == "--":
            return words[index + 1 :]
        if word.startswith("--"):
            if word not in long_flags:
                return None
            index += 1
            continue
        if word.startswith("-") and word != "-":
            if not is_allowed_short_flag_group(word, short_flags):
                return None
            index += 1
            continue
        return words[index:]
    return []


def safe_ls(words, cwd, roots):
    paths = options_and_paths(words[1:], LS_SHORT_FLAGS, LS_LONG_FLAGS)
    return paths is not None and paths_are_safe(paths, cwd, roots, allow_directories=True, require_path=False)


def safe_cat(words, cwd, roots):
    paths = options_and_paths(words[1:], CAT_SHORT_FLAGS, frozenset())
    return paths is not None and paths_are_safe(paths, cwd, roots, allow_directories=False)


def count_option_value_is_safe(word):
    return bool(COUNT.fullmatch(word))


def safe_head_or_tail(words, cwd, roots):
    arguments = words[1:]
    index = 0
    while index < len(arguments):
        word = arguments[index]
        if word == "--":
            return paths_are_safe(arguments[index + 1 :], cwd, roots, allow_directories=False)
        if word in {"-n", "-c", "--lines", "--bytes"}:
            if index + 1 >= len(arguments) or not count_option_value_is_safe(arguments[index + 1]):
                return False
            index += 2
            continue
        if word.startswith("-n") or word.startswith("-c"):
            if not count_option_value_is_safe(word[2:]):
                return False
            index += 1
            continue
        if word.startswith("--lines=") or word.startswith("--bytes="):
            if not count_option_value_is_safe(word.split("=", 1)[1]):
                return False
            index += 1
            continue
        if word.startswith("-") and word != "-":
            if not is_allowed_short_flag_group(word, HEAD_TAIL_SHORT_FLAGS):
                return False
            index += 1
            continue
        return paths_are_safe(arguments[index:], cwd, roots, allow_directories=False)
    return False


def safe_wc(words, cwd, roots):
    paths = options_and_paths(words[1:], WC_SHORT_FLAGS, frozenset())
    return paths is not None and paths_are_safe(paths, cwd, roots, allow_directories=False)


def safe_search(words, cwd, roots):
    arguments = words[1:]
    patterns = 0
    no_config = words[0] != "rg"
    index = 0
    while index < len(arguments):
        word = arguments[index]
        if word == "--":
            remaining = arguments[index + 1 :]
            if not patterns:
                if not remaining:
                    return False
                patterns = 1
                remaining = remaining[1:]
            return no_config and paths_are_safe(remaining, cwd, roots, allow_directories=True)
        if word in {"-e", "--regexp"}:
            if index + 1 >= len(arguments) or not arguments[index + 1]:
                return False
            patterns += 1
            index += 2
            continue
        if word.startswith("--regexp="):
            if not word.split("=", 1)[1]:
                return False
            patterns += 1
            index += 1
            continue
        if word.startswith("--"):
            if word not in SEARCH_LONG_FLAGS:
                return False
            if word == "--no-config":
                no_config = True
            index += 1
            continue
        if word.startswith("-") and word != "-":
            if not is_allowed_short_flag_group(word, SEARCH_SHORT_FLAGS):
                return False
            index += 1
            continue
        if not patterns:
            patterns = 1
            index += 1
            continue
        return no_config and paths_are_safe(arguments[index:], cwd, roots, allow_directories=True)
    return False


def safe_git(words):
    if len(words) < 3 or words[1] != "--no-pager" or words[2] not in GIT_FLAGS:
        return False
    flags = words[3:]
    if any(word not in GIT_FLAGS[words[2]] for word in flags):
        return False
    return GIT_REQUIRED_SAFETY_FLAGS.issubset(flags)


def environment_has_git_override():
    return any(name in os.environ for name in GIT_ENVIRONMENT_VARIABLES)


def bash_command_is_safe(command, event):
    words = command_words(command)
    if words is None:
        return False
    directories = directories_for(event)
    if directories is None:
        return False
    cwd, roots = directories
    executable = words[0]
    if executable == "pwd":
        return len(words) == 1
    if executable == "ls":
        return safe_ls(words, cwd, roots)
    if executable == "cat":
        return safe_cat(words, cwd, roots)
    if executable in {"head", "tail"}:
        return safe_head_or_tail(words, cwd, roots)
    if executable == "wc":
        return safe_wc(words, cwd, roots)
    if executable in {"grep", "rg"}:
        return safe_search(words, cwd, roots)
    if executable == "git":
        return not environment_has_git_override() and safe_git(words)
    return False


def permission_settings_paths(event):
    directories = []
    cwd = event.get("cwd")
    if isinstance(cwd, str) and Path(cwd).is_absolute():
        directories.extend((Path(cwd), *Path(cwd).parents))
    project_directory = os.environ.get("CLAUDE_PROJECT_DIR")
    if project_directory and Path(project_directory).is_absolute():
        directories.append(Path(project_directory))

    paths = [Path.home() / ".claude" / "settings.json"]
    for directory in directories:
        paths.extend(
            (
                directory / ".claude" / "settings.json",
                directory / ".claude" / "settings.local.json",
            )
        )
    return tuple(dict.fromkeys(paths))


def permission_rule_tool_name(rule):
    if not isinstance(rule, str):
        return ""
    return rule.split("(", 1)[0]


def has_explicit_native_permission_rule(tool_name, event):
    for settings_path in permission_settings_paths(event):
        try:
            with settings_path.open(encoding="utf-8") as settings_file:
                settings = json.load(settings_file)
        except (OSError, ValueError):
            continue
        permissions = settings.get("permissions") if isinstance(settings, dict) else None
        if not isinstance(permissions, dict):
            continue
        for behavior in ("ask", "deny"):
            rules = permissions.get(behavior)
            if isinstance(rules, list) and any(
                permission_rule_tool_name(rule) == tool_name for rule in rules
            ):
                return True
    return False


def decision_for_event(event):
    if not isinstance(event, dict):
        return None
    if event.get("hook_event_name") != PRE_TOOL_USE or event.get("permission_mode") != PLAN_MODE:
        return None
    tool_name = event.get("tool_name")
    tool_input = event.get("tool_input")
    if not isinstance(tool_name, str) or not isinstance(tool_input, dict):
        return None
    if has_explicit_native_permission_rule(tool_name, event):
        return None
    if tool_name in AUTO_APPROVED_TOOLS:
        return {"hookSpecificOutput": {"permissionDecision": "allow"}}
    if tool_name != "Bash" or not bash_command_is_safe(tool_input.get("command"), event):
        return None
    return {"hookSpecificOutput": {"permissionDecision": "allow"}}


def main():
    try:
        event = json.load(sys.stdin)
    except (json.JSONDecodeError, OSError, ValueError):
        return 0
    decision = decision_for_event(event)
    if decision is not None:
        print(json.dumps(decision, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
