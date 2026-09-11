---
name: db-inspect
description: Read MongoDB through a temp Node script that loads .env, connects, queries, then deletes itself. Use on /db-inspect, or whenever you need to see the state of a collection while debugging, instead of hand-writing a script each time.
---

# DB Inspect

Quick, safe, repeatable MongoDB **reads**, in place of the write-script → run → error → rewrite cycle.

## Usage

```
/db-inspect <collection> [query]
```

The collection names below are placeholders. Pass whatever the `listCollections` run actually returned, never a name you assumed the project has.

- `/db-inspect` → list the collections
- `/db-inspect users { email: "someone@example.com" }`
- `/db-inspect <collection> { userId: "abc123", status: "completed" }`
- `/db-inspect <collection> { name: /keyword/i }`

## How it works

read the connection URI and the database name from the project's `.env`, under the variable names that file already uses → write the script to `/tmp/db-inspect-<timestamp>.mjs` → run it from the repo root with a runner the project already has (`npx tsx`, `node`, `bun`) → delete the script the moment the run ends, success or failure alike → report the result

**Never hardcode a DB name.**

```javascript
import { MongoClient } from 'mongodb';
import { config } from 'dotenv';
import { resolve } from 'path';

config({ path: resolve('.env') });

// this pair is the common naming; use whichever names the project's .env holds
const client = new MongoClient(process.env.MONGODB_URI);
try {
  await client.connect();
  const db = client.db(process.env.MONGODB_DB);

  // no collection → listCollections
  // collection + query → find
  // collection alone → count + sample

  const result = await db.collection('<collection>')
    .find(<query>)
    .limit(10)
    .toArray();

  console.log(JSON.stringify(result, null, 2));
} finally {
  await client.close();
}
```

## Rules

- **Read-only.** `find`, `aggregate`, `count`, `listCollections`, and nothing else. A write / update / delete → refuse, and tell the user to ask for it explicitly.
- **`limit(10)` by default**, and the user can override it.
- **A 24-hex-char string is an ObjectId.** Wrap it in `new ObjectId('…')` and add the import.
- **Never dump a huge document.** Over 50 fields, or a nested array over 20 items → report the identifying fields plus the ones the user asked about.

## When it fails

- **Connection refused** → does the project's `.env` exist, and does it hold the URI variable the script read?
- **Collection not found** → list the collections, suggest the closest name.
- **Query syntax error** → usually a missing quote or a wrong field name, fix it and retry **once**.
