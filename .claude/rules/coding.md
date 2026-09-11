---
paths:
  - "**/*.{ts,tsx,js,jsx,mjs,cjs}"
  - "**/*.dart"
  - "**/*.{py,sh}"
---

# Writing code

Loaded whenever you read or write a source file. The hard gates in
[CLAUDE.md](../../CLAUDE.md) still bind — this file adds what only applies to code.

## Code standard

1. SOLID + DRY; small single-responsibility functions; extract helpers; names that say what the thing
   does, in full words
2. Concise — no fluff, no over-engineering; spec exact: no unrequested fields / params / abstractions
3. Early return over nested if-else; switch-case over long if-else chains
4. **Reaching for a comment to explain what a block does → extract that block into a function whose name
   says it.** The name is the explanation. Delete logic-explaining comments; keep reference notes (URLs,
   magic numbers, data formats)
5. Refactor messy code you touch for clarity — never change behavior
6. Package manager: match the lockfile; never switch
7. Design for the next change: right seams, no speculative abstractions
8. Commits: no `Co-Authored-By`

## Verifying

Run what proves it — typecheck, lint, the targeted tests — and paste the pass/fail lines, never the
exit code alone. Nothing runnable → re-read every changed call path from disk. The regression check is
mandatory: the scenario that worked _before_ the fix has to still work after it.

A `// temporary` / `// backward-compat` / `// migration` comment is the previous author saying that path
is a stopgap. It marks a gap, never closes one — run that path against what was asked before you call
the code done.

## Browser

**Playwright is off by default** — a browser only when the user permits it, or a specific bug needs one.
The rules for driving it live in [the playwright skill](../skills/playwright/SKILL.md).
