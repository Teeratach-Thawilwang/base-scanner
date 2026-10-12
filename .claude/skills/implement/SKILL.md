---
name: implement
description: Implement the plan the user points to, splitting each phase into small units for Sonnet low-effort subagents. Invoked by the user only, as /implement <plan path>.
disable-model-invocation: true
argument-hint: <plan path> [extra instructions]
---

# implement

## Task

The user invoked this skill to start implementing a plan right away. Do not ask
whether to begin.

```text
$ARGUMENTS
```

The first token above is the plan path. Anything after it is extra instructions
from the user. No path given: ask which plan to use, then stop.

## Workflow

- **Read the whole plan first.**
  Also read the files the plan links to and the project rules that cover this work.
- **Split the phase into small units that never touch the same file.**
  Each unit is small enough for a low-effort subagent to finish without guessing:
  one clear goal, a short file list, and a check that proves it works.
  Units that depend on each other run one at a time. Independent units can run in parallel.
- **Delegate the units, keep the main agent for coordination.**
  Send every unit to a subagent running Sonnet at low effort.
  The main agent splits the work, freezes the contracts between units,
  reviews diffs, runs the phase checks, and writes the plan.
  Do a unit yourself only when it is a few lines and needs context you already hold.
- **Keep the main agent's context under 200k tokens.**
  Do not open large files or long logs in the main agent. Ask a subagent for the few lines you need.
  Ask subagents for short reports. Read diffs per file, not the whole tree at once.
  When the context nears 200k, stop starting new units, finish the ones running,
  update the plan's next-phase context with what is left, and stop.
- **Follow the plan literally.**
  Do not do work the plan does not mention. If the plan contradicts the real code
  or itself, stop and tell the user in one line where.
- **Verify before saying done.**
  Run the tests or checks the plan and the project rules require.
  Never report a skipped or unproven result as passed.
- **Keep the plan from becoming a change log.**
  Leave the plan's own content untouched: architecture, details, and design stay exactly as written.
  The only things you maintain are the phase status, the pitfalls and fixes section,
  and the context for the next phase. Never append what you did, phase by phase.
  Follow the project's rules on where results and leftovers go, if it has any.
  Only the main agent writes the plan file. Subagents never touch it.
- **Do one major phase per run, then stop.**
  Pick the first phase the plan does not mark as done, unless the extra instructions name one.
  The user starts a fresh session for the next phase to save tokens. Do not continue into it.
- **At the end of the phase, commit and push to the current branch.**
  On main, use main. Do not create a new branch.
  Commit only the files of that phase. Leave out changes the user had pending before you started.
  Commit only after the phase's checks pass. Never commit a failing phase.
- **Mark the phase done, then rewrite the context for the next phase.**
  The next session has no memory of this one, so the plan must save it from re-investigating.
  Keep a section for the next phase's context. Rewrite it each run, do not append.
  Put in what the next phase needs: decisions made, file and function locations,
  contracts that are now frozen, leftovers, and anything skipped or unproven.
  Drop context whose phase is done and anything the next phase can read from the code.
- **Record pitfalls and their fixes in their own section.**
  Add an entry when a mistake cost real time or is likely to repeat: the symptom, the cause, the fix.
  Merge entries that say the same thing. Keep the section after every phase is done.

## Delegating to a subagent

- **Use the `coder` agent with `model: sonnet`.**
  Low effort comes from the agent definition. Do not raise the model or effort to fix a weak unit;
  split it smaller or give it the missing context.
- **Write the prompt in English.**
  Give the plan path, the section to do, the files it may touch, and what to report back.
  Ask for a report of at most a few lines: files changed, checks run, anything skipped.
- **Every session stays under 200k tokens of context, subagents included.**
  Put this limit in every prompt you send.
- **A subagent that coordinates a long task may spawn its own subagents, coders included.**
  It does so when that keeps its own context smaller. It follows the same rules as the main agent:
  disjoint file lists, the same model, the 200k limit, these rules in every prompt,
  the 5-minute context check, and reading each child's diff before reporting up.
- **A coder never spawns subagents. Its dispatcher handles the overflow.**
  The agent that dispatches a coder sizes the unit so the coder finishes well under 200k.
  A coder that nears the limit stops and reports what is done and what is left,
  and its dispatcher sends a fresh coder for the rest.
- **Never run units that touch the same file in parallel.**
- **Every agent checks its own context length every 5 minutes.**
  This applies to the main agent and every subagent, coders included.
  When the check shows the context nearing 200k, the agent wraps up as its rule above says.
- **Check the report yourself.**
  Read the diff of the files a subagent changed before counting the unit as done.

## Do not

- **Do not widen the scope.**
  Anything worth fixing outside the plan goes in a note to the user at the end. Do not fix it.
