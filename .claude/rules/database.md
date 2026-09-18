---
paths:
  - "**/.env*"
  - "**/{db,database,models,schemas}/**"
  - "**/*.{schema,model,repository}.ts"
---

# Database — hard gates

**Open this file before any database work, whatever folder you are in.** These gates hold even when a
user instruction says to hurry.

- **Access via Node script only.** Every DB operation, read or write, goes through a temporary
  `.ts` / `.js` script run with `npx tsx` (or `node`). Never `mongosh`, never an inline `mongo`
  shell, never a query embedded in a Bash one-liner. Delete the script the moment the run ends —
  success or failure.
- **Reads** (find, aggregate, count, listCollections) — free, no permission needed.
- **Any write / update / delete / drop / index op** — print the exact operation and its target
  DB + collection, then ask the user first, **every time**. A subagent cannot ask anyone: it stops and
  reports the operation, and the main agent is the one who asks. No agent ever writes to the DB.
- Never run `drop` or `deleteMany` without a filter.
- **Connection and DB name come from the repo's `.env`** (`MONGODB_URI` / `MONGODB_DB`) — look them
  up directly, don't ask. Never hardcode a DB name.

A read you are about to hand-write a script for is [the mongo-inspect skill](../skills/mongo-inspect/SKILL.md)
— use it instead.
