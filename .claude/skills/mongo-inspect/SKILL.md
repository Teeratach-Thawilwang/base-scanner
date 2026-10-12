---
name: mongo-inspect
description: Read MongoDB through a throwaway script that connects, queries, prints, then deletes itself. Use on /mongo-inspect, or whenever you need to see what a collection holds while debugging, instead of hand-writing a script each time.
---

# Mongo Inspect

Fast, read-only MongoDB queries in any project.

## Usage

```
/mongo-inspect                         list collections
/mongo-inspect <collection>            count + 3 sample docs
/mongo-inspect <collection> <filter>   find, limit 10
```

Examples, all made up:

```
/mongo-inspect orders { status: "paid" }
/mongo-inspect orders { _id: "65f1c2a9e4b0a1b2c3d4e5f6" }
/mongo-inspect products { name: /lamp/i }
```

## Find the connection

Don't assume variable names. Check, in order:

1. Env files at the repo root: `.env.local`, `.env.development`, `.env`.
   Grep for a value starting with `mongodb://` or `mongodb+srv://`.
2. A var holding the DB name, e.g. `*_DB`, `*_DATABASE`, `*_DB_NAME`.
3. No DB var → use the DB in the URI path. Neither → list databases
   and ask.

Never print the URI. It carries credentials.

## Run it

Use Node with the project's own `mongodb` driver, never `mongosh`. The script
must live **inside the repo root**, not `/tmp`, or `import 'mongodb'`
won't resolve against the project's `node_modules`.

```
write   <repo>/.mongo-inspect-<timestamp>.mjs
run     node --env-file=<env file> .mongo-inspect-<timestamp>.mjs
delete  the script, success or failure
```

```javascript
import { MongoClient, ObjectId } from 'mongodb';

const client = new MongoClient(process.env.<URI_VAR>);
try {
  await client.connect();
  const db = client.db(process.env.<DB_VAR>); // omit arg to use the URI's DB

  const result = await db.collection('<collection>')
    .find(<filter>)
    .limit(10)
    .toArray();

  console.log(JSON.stringify(result, null, 2));
} finally {
  await client.close();
}
```

No `mongodb` package in the project → tell the user, don't install
anything.

## Rules

- **Read-only.**
  `find`, `findOne`, `aggregate`, `countDocuments`, `listCollections`,
  `listDatabases`. Anything that writes → refuse; the user must ask
  explicitly.
- **`aggregate` with `$out` or `$merge` counts as a write.**
- **Limit 10 by default.** The user can override.
- **Convert ids before querying.** See the next section.
- **Trim large docs.**
  Over 50 fields, or an array over 20 items → show the identifying fields
  plus the ones asked about.
- **Mask secrets in output.**
  Fields like `password`, `token`, `secret`, `apiKey` → `"***"`.

## ObjectId

The user pastes ids as plain text. MongoDB never matches the string
`"65f1…"` against an ObjectId, so an unconverted id returns nothing.

- **Every 24-hex value in the filter is a candidate id.**
  `/^[0-9a-f]{24}$/i`, in any field, at any depth, including inside `$in`.
- **Match both forms.** A field may store the id as an ObjectId or as a
  string, so replace each candidate with:
  `{ $in: [new ObjectId('<hex>'), '<hex>'] }`
- **A bare id with no field means `_id`.**
  `/mongo-inspect orders 65f1c2a9e4b0a1b2c3d4e5f6` →
  `{ _id: { $in: [new ObjectId('65f1…'), '65f1…'] } }`
- **Zero results is not "not found" yet.**
  Before reporting, check that the conversion ran, and say in the reply
  which forms were tried.

## When it fails

- **Can't connect**: wrong env file, missing var, or the DB isn't running.
  Say which.
- **Collection not found**: list collections, suggest the closest name.
- **Query syntax error**: fix and retry once, then stop and show the error.
