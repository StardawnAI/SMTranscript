# Text library

A Cloudflare Worker that keeps each customer's transcripts and post texts as text, in a
database of the customer's own: one SQLite Durable Object per customer, with FTS5 full-text
search. The SMTranscript MCP server writes to it and reads from it when
`SMTRANSCRIPT_LIBRARY_URL` and `SMTRANSCRIPT_LIBRARY_KEY` are set (see the main README).
No pictures, picture links or videos are stored.

Deployed at `https://smtranscript-library.jim-presting.workers.dev` (Cloudflare account
`8d230a3cee37c5b1af5cc5cffad91030`). The operator's admin token and the issued keys are kept
outside the repository.

## Starting a new customer

1. The operator creates the customer and gets its key, once:

   ```bash
   curl -s -X POST https://smtranscript-library.jim-presting.workers.dev/v1/admin/customers \
     -H "Authorization: Bearer $ADMIN_TOKEN" -H "Content-Type: application/json" \
     -d '{"name": "Customer GmbH"}'
   ```

   The answer is `{"customer": "...", "key": "sml_...", ...}`. Hand the key to the customer
   over a private channel; it is not shown again.

2. The customer gives the SMTranscript server the two variables, then restarts the app:

   - Claude Code with the `awg-smtranscript` plugin: in the `env` block of
     `~/.claude/settings.json`:

     ```json
     "env": {
       "SMTRANSCRIPT_LIBRARY_URL": "https://smtranscript-library.jim-presting.workers.dev",
       "SMTRANSCRIPT_LIBRARY_KEY": "sml_..."
     }
     ```

   - Codex and Grok Build: under `[mcp_servers.smtranscript]` in their `config.toml`:

     ```toml
     env = { SMTRANSCRIPT_LIBRARY_URL = "https://smtranscript-library.jim-presting.workers.dev", SMTRANSCRIPT_LIBRARY_KEY = "sml_..." }
     ```

   - Cursor and Antigravity: an `"env"` object with the same two entries next to `"command"`
     in the `smtranscript` entry of their `mcp.json`.

3. After a restart of the client, the tools list shows `library_search`, `library_list`,
   `library_get` and `library_delete`. Every transcript fetched from then on is kept; its
   answer has `"library": {"kept": 1}`.

A lost key cannot be shown again: revoke the customer and create a new one. Revoking keeps
the texts unless `?erase=true` is given.

## Keys

A customer key is `sml_<customer>_<secret>`. The customer part picks the database; the
database holds only the SHA-256 of the secret and checks it. The worker holds only the
SHA-256 of the admin token (secret `ADMIN_TOKEN_SHA256`).

## API

Customer routes take `Authorization: Bearer sml_...`:

| Route                                            | What it does                                                                                                                                                                                                                                     |
| ------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `POST /v1/items`                                 | Keeps 1 to 50 items `{url, text, platform, kind, external_id, title, author, published_at, duration_seconds, language, segments, meta}`. The same `url` again replaces its item. Answers `{saved, rejected}`, each rejected item with its reason |
| `GET /v1/items?platform=&author=&limit=&before=` | Lists items newest first, without their text; `next_before` pages on                                                                                                                                                                             |
| `GET /v1/item?id=` or `?url=`                    | One item in full                                                                                                                                                                                                                                 |
| `DELETE /v1/item?id=` or `?url=`                 | Deletes one item                                                                                                                                                                                                                                 |
| `GET /v1/search?q=&platform=&limit=`             | Full-text search; every word is a quoted prefix term, so no input is read as FTS5 syntax                                                                                                                                                         |
| `GET /v1/usage`                                  | Items, bytes, requests today and the limits                                                                                                                                                                                                      |

Operator routes take `Authorization: Bearer <admin token>`:

| Route                               | What it does                                                     |
| ----------------------------------- | ---------------------------------------------------------------- |
| `POST /v1/admin/customers` `{name}` | Creates a customer; the answer carries its key, once             |
| `GET /v1/admin/customers`           | Lists customers: id, name, dates; never keys                     |
| `DELETE /v1/admin/customers/<id>`   | Revokes the customer's key; `?erase=true` also deletes its texts |

## Limits

Set as variables in `wrangler.jsonc`:

| Variable             | Default     | Meaning                                                                          |
| -------------------- | ----------- | -------------------------------------------------------------------------------- |
| `MAX_ITEM_BYTES`     | 1,000,000   | One item, text and segments together                                             |
| `MAX_CUSTOMER_BYTES` | 200,000,000 | One customer's library; beyond it new items are refused as "the library is full" |
| `MAX_DAILY_REQUESTS` | 20,000      | Requests per customer per UTC day; beyond it 429                                 |

A request body is at most 5 MB.

## Cost

Measured on 2026-11-11 in the worker's database: a reel transcript averages about 800
characters of text and 1,150 of timed segments, a picture description about 3,400. With the
search index that is roughly 3 to 4 KB per reel. Cloudflare's price for SQLite in Durable
Objects on Workers Paid (checked 2026-11-11): 5 GB-month included, then USD 0.20 per
GB-month; 50 million rows written and 25 billion rows read a month included. A million
kept reels fit in the included storage.

## Develop

`node --test library/worker.test.mjs` runs the worker against `node:sqlite`, which stands in
for the Durable Object's SQLite (`fake-cloudflare.mjs`), so schema, triggers and FTS5 run
for real.
