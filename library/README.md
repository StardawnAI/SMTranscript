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
| `POST /v1/items` | Keeps 1 to 50 items `{url, text, platform, kind, external_id, title, author, published_at, duration_seconds, language, segments, meta}`. The same `url` again replaces its item; the same content again writes nothing and answers `unchanged: true`. Answers `{saved, rejected, rows_written}`, each rejected item with its reason |
| `GET /v1/items?platform=&author=&limit=&before=` | Lists items newest first, without their text; `next_before` pages on                                                                                                                                                                             |
| `GET /v1/item?id=`, `?url=` or `?platform=&external_id=` | One item in full |
| `DELETE /v1/item?id=`, `?url=` or `?platform=&external_id=` | Deletes one item |
| `GET /v1/search?q=&platform=&limit=`             | Full-text search; every word is a quoted prefix term, so no input is read as FTS5 syntax                                                                                                                                                         |
| `GET /v1/usage` | Items, bytes of content, `database_bytes` (the size Cloudflare bills), requests today and the limits |

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

A request body is at most 5 MB. The day's request count is kept in memory and written every
50 requests, so counting does not itself write a row per request; an object Cloudflare
evicts can forget up to 49 of them.

## Cost

Measured on 2026-11-11 against the deployed worker with 100 real reel transcripts
(1,176 characters of text on average, with timed segments), and confirmed by Cloudflare's
own analytics for that hour (446 rows written):

| Per reel | Measured |
| --- | --- |
| Rows written, new reel | 4 |
| Rows written, same reel again | 0 |
| Rows written, changed text | 4 |
| Database size | about 5.5 KB (552,960 bytes for 100) |
| Active time per request | about 10 ms |

Cloudflare's prices on Workers Paid (USD 5 a month, checked 2026-11-11):

| | Included a month | Beyond |
| --- | --- | --- |
| Durable Object rows written | 50 million | USD 1.00 per million |
| Durable Object storage | 5 GB | USD 0.20 per GB |
| Durable Object requests | 1 million | USD 0.15 per million |
| Worker requests | 10 million | USD 0.30 per million |

A new reel costs two requests (look up, keep) and 4 rows. A million new reels a month are
4 million rows and 2 million requests of each kind: inside the included amounts but for
1 million Durable Object requests (USD 0.15) and 0.5 GB of storage (USD 0.10). Rows become
billed only beyond 12.5 million new reels a month, at USD 4 per further million.

## Develop

`node --test library/worker.test.mjs` runs the worker against `node:sqlite`, which stands in
for the Durable Object's SQLite (`fake-cloudflare.mjs`), so schema, triggers and FTS5 run
for real.
