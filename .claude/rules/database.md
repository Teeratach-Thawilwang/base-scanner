---
paths:
  - "**/.env*"
  - "**/{db,database,models,schemas,migrations}/**"
  - "**/*.{schema,model,repository,db}.{ts,js}"
---

# Database

## Hard gates

These hold for every database engine, and even when the user says to hurry.

- **Access through a throwaway script only.**
  Write it in the project's language, with the driver the project already
  depends on. No DB CLI, no query inside a Bash one-liner. Delete the script
  the moment the run ends, success or failure.
- **Reads are free.**
  Select, find, count, aggregate, list tables or collections, read the schema.
- **Every write needs the user's yes, every time.**
  Insert, update, delete, drop, migration, index change. Show the exact
  statement and its target database and table first.
- **Subagents never write.**
  A subagent can't ask the user. It stops and reports the operation, and the
  main agent asks.
- **No `UPDATE` or `DELETE` without a filter. Never drop a table or
  collection.**

## Which database is live

- **Use the connection the running app uses.**
  Find it the way the app finds it: env vars, config, or a path helper in the
  code. Look it up, don't ask, never hardcode it.
- **A database file in the repo is not proof it's the live one.**
  Check where the app actually points before reading it.
- **Never print a connection string.** It carries credentials.

## Reading safely

- **Open read-only when the driver allows it.**
  For SQLite, open the file with the read-only flag.
- **Close the connection before the script exits.**
  A crashed script must not leave a lock on a file the app is holding.
- **Limit results.** Default to 10 rows unless the user asks for more.
- **Mask secrets in output.** Passwords, tokens, API keys → `"***"`.
- **MongoDB reads go through [mongo-inspect](../skills/mongo-inspect/SKILL.md).**
  Don't hand-write a script for them.
