// The customer's text library: every transcript and post text a customer's
// SMTranscript MCP fetched, kept as text in that customer's own SQLite database
// (one Durable Object per customer) and searchable with FTS5. No media is stored.
//
// A customer key looks like `sml_<customer>_<secret>`. The customer's object
// keeps only the SHA-256 of the secret: the key is shown once, when the customer
// is created, and never again.

const KEY = /^sml_([a-z0-9]{16})_([a-f0-9]{48})$/;
const MAX_BODY = 5 * 1024 * 1024;
const MAX_BATCH = 50;
// A schema's definition and one text's result: big enough for a deep,
// many-level schema, small enough that a runaway answer cannot fill a library.
const SCHEMA_CHARS = 20000;
const RESULT_BYTES = 20000;
// A result or definition kept as JSON comes back as JSON; anything else as the text it was.
const parseKept = (text) => {
  try {
    return JSON.parse(text);
  } catch {
    return text;
  }
};

const num = (value, fallback) => (Number(value) > 0 ? Number(value) : fallback);
export const limits = (env = {}) => ({
  itemBytes: num(env.MAX_ITEM_BYTES, 1_000_000),
  customerBytes: num(env.MAX_CUSTOMER_BYTES, 200_000_000),
  dailyRequests: num(env.MAX_DAILY_REQUESTS, 20_000),
});

const json = (status, body) => new Response(JSON.stringify(body), {
  status, headers: { 'content-type': 'application/json', 'cache-control': 'no-store' },
});
const fail = (status, error) => json(status, { error });
const now = () => new Date().toISOString();
const utf8 = (s) => new TextEncoder().encode(s || '').length;

async function sha256(text) {
  const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(text));
  return [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, '0')).join('');
}

function sameText(a, b) {
  if (typeof a !== 'string' || typeof b !== 'string' || a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i += 1) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}

function randomId(length, alphabet) {
  const bytes = crypto.getRandomValues(new Uint8Array(length));
  return [...bytes].map((b) => alphabet[b % alphabet.length]).join('');
}

const bearer = (request) => (request.headers.get('authorization') || '').replace(/^Bearer\s+/i, '').trim();

// A request the library refuses, with the status that says why.
class Refused extends Error {
  constructor(status, message) {
    super(message);
    this.status = status;
  }
}

async function readJson(request) {
  if (Number(request.headers.get('content-length') || 0) > MAX_BODY) throw new Refused(413, 'body over 5 MB');
  const text = await request.text();
  if (utf8(text) > MAX_BODY) throw new Refused(413, 'body over 5 MB');
  try {
    return JSON.parse(text || '{}');
  } catch {
    throw new Refused(400, 'body is not JSON');
  }
}

// A folder name: what a person calls a group of texts ("Immobilien", "General").
// Trimmed, inner spaces collapsed, no control characters, up to 80 characters.
export const GENERAL = 'General';
export function folderName(raw) {
  if (raw === undefined || raw === null) return null;
  const name = String(raw).replace(/[\u0000-\u001f\u007f]/g, '').replace(/\s+/g, ' ').trim().slice(0, 80);
  return name || null;
}

const short = (value, max) => (value === undefined || value === null || value === '' ? null : String(value).slice(0, max));

// One item as it is stored, or the reason it is not.
export function normalize(raw, itemBytes) {
  if (!raw || typeof raw !== 'object') return { reason: 'not an object' };
  const url = String(raw.url || '').trim();
  if (!/^https?:\/\/\S+$/i.test(url) || url.length > 2048) return { reason: 'url must be an http(s) link up to 2048 characters' };
  const text = typeof raw.text === 'string' ? raw.text.trim() : '';
  if (!text) return { url, reason: 'no text to keep' };
  const segments = Array.isArray(raw.segments)
    ? raw.segments.slice(0, 20000).map((s) => ({ start_ms: Number(s?.start_ms) || 0, end_ms: Number(s?.end_ms) || 0, text: String(s?.text || '') }))
    : null;
  const meta = raw.meta && typeof raw.meta === 'object' && !Array.isArray(raw.meta) ? JSON.stringify(raw.meta) : null;
  if (meta && meta.length > 10000) return { url, reason: 'meta over 10000 characters' };
  const duration = raw.duration_seconds === null || raw.duration_seconds === undefined ? NaN : Number(raw.duration_seconds);
  const item = {
    url,
    platform: /^[a-z]{2,32}$/.test(raw.platform || '') ? raw.platform : 'other',
    kind: raw.kind === 'post' ? 'post' : 'video',
    external_id: short(raw.external_id, 200),
    title: short(raw.title, 500),
    author: short(raw.author, 200),
    published_at: short(raw.published_at, 40),
    duration_seconds: duration >= 0 ? duration : null,
    language: short(raw.language, 16),
    text,
    segments: segments ? JSON.stringify(segments) : null,
    meta,
    folder: folderName(raw.folder),
  };
  item.bytes = utf8(item.text) + utf8(item.segments) + utf8(item.meta) + utf8(item.title) + utf8(item.author) + utf8(url);
  if (item.bytes > itemBytes) return { url, reason: `over ${itemBytes} bytes` };
  return { item };
}

// Search words become quoted prefix terms, so nothing a person types can be
// read as FTS5 syntax.
export function ftsQuery(q) {
  const words = String(q || '').normalize('NFC').match(/[\p{L}\p{N}]+/gu) || [];
  return words.slice(0, 10).map((w) => `"${w}"*`).join(' ');
}

const SCHEMA = [
  'create table if not exists account (id integer primary key check (id = 1), hash text not null, name text,'
    + ' created_at text not null, revoked_at text)',
  'create table if not exists item (id integer primary key, url text not null unique, platform text, kind text,'
    + ' external_id text, title text, author text, published_at text, duration_seconds real, language text,'
    + ' text text not null, segments text, meta text, bytes integer not null, created_at text not null, updated_at text not null)',
  "create virtual table if not exists item_fts using fts5(title, author, text, content='item', content_rowid='id',"
    + " tokenize='unicode61 remove_diacritics 2')",
  'create trigger if not exists item_ai after insert on item begin'
    + ' insert into item_fts(rowid, title, author, text) values (new.id, new.title, new.author, new.text); end',
  'create trigger if not exists item_ad after delete on item begin'
    + " insert into item_fts(item_fts, rowid, title, author, text) values ('delete', old.id, old.title, old.author, old.text); end",
  'create trigger if not exists item_au after update of title, author, text on item begin'
    + " insert into item_fts(item_fts, rowid, title, author, text) values ('delete', old.id, old.title, old.author, old.text);"
    + ' insert into item_fts(rowid, title, author, text) values (new.id, new.title, new.author, new.text); end',
  'create table if not exists usage_day (day text primary key, requests integer not null)',
  // What the customer's chat model is to pull out of every text of a folder, and
  // what it pulled out, one result per text. The model doing it is the
  // customer's own (Claude or ChatGPT); the library only keeps both.
  'create table if not exists extract_schema (name text primary key collate nocase, folder text not null,'
    + ' description text, definition text not null, created_at text not null, updated_at text not null)',
  'create table if not exists extraction (schema_name text not null collate nocase, item_id integer not null,'
    + ' result text not null, bytes integer not null, updated_at text not null, primary key (schema_name, item_id))',
  'create trigger if not exists item_ad_extraction after delete on item begin'
    + ' delete from extraction where item_id = old.id; end',
];
// Added after the first deploy; databases made before get them in schema().
const ADDED = [
  ['sha', 'alter table item add column sha text'],
  ['folder', `alter table item add column folder text not null default '${GENERAL}'`],
];
const INDEXES = [
  'create index if not exists item_external on item (platform, external_id)',
  'create index if not exists item_folder on item (folder collate nocase)',
];
// How often the day's request count is written: counting every request would
// itself be a row written per request, reads included.
const COUNT_EVERY = 50;

const LIST_COLUMNS = 'id, url, folder, platform, kind, external_id, title, author, published_at, duration_seconds, language,'
  + ' length(text) as characters, created_at, updated_at';

// One customer's database.
export class Library {
  constructor(ctx, env) {
    this.ctx = ctx;
    this.env = env;
    this.limits = limits(env);
    // Only a customer the operator created has tables. A key-shaped token with
    // an invented customer id reaches an empty object, which stays empty:
    // nothing is written, so nothing is kept or billed for it.
    this.ready = this.exists();
    if (this.ready) this.schema();
  }

  exists() {
    return this.ctx.storage.sql.exec("select 1 from sqlite_master where type = 'table' and name = 'account'").toArray().length > 0;
  }

  schema() {
    for (const statement of SCHEMA) this.ctx.storage.sql.exec(statement);
    const columns = this.ctx.storage.sql.exec("select name from pragma_table_info('item')").toArray().map((c) => c.name);
    for (const [column, statement] of ADDED) if (!columns.includes(column)) this.ctx.storage.sql.exec(statement);
    for (const statement of INDEXES) this.ctx.storage.sql.exec(statement);
    // Libraries from before folders reindexed a text on every change, a move
    // too; now only a change of its title, author or text does. Once per library.
    const trigger = this.ctx.storage.sql.exec("select sql from sqlite_master where type = 'trigger' and name = 'item_au'").toArray()[0];
    if (trigger && !/update of/i.test(trigger.sql)) {
      this.ctx.storage.sql.exec('drop trigger item_au');
      this.ctx.storage.sql.exec(SCHEMA.find((x) => x.includes('item_au')));
    }
    this.day = null;
    this.total = null;
  }

  // The library's size, read once per object and kept up to date in memory:
  // summing the table on every save would read every row each time.
  size() {
    if (this.total === null) {
      this.total = this.rows('select (select coalesce(sum(bytes), 0) from item)'
        + ' + (select coalesce(sum(bytes), 0) from extraction) as total')[0].total;
    }
    return this.total;
  }

  // Every query goes through here, so the rows it wrote (what Cloudflare bills)
  // can be counted per request.
  rows(query, ...bindings) {
    const cursor = this.ctx.storage.sql.exec(query, ...bindings.map((b) => (b === undefined ? null : b)));
    const out = cursor.toArray();
    this.written = (this.written || 0) + (cursor.rowsWritten || 0);
    return out;
  }

  async fetch(request) {
    const url = new URL(request.url);
    try {
      if (url.pathname.startsWith('/internal/')) return await this.internal(url.pathname, await readJson(request));
      const denied = await this.authenticate(request);
      if (denied) return denied;
      return await this.route(request, url);
    } catch (e) {
      if (e instanceof Refused) return fail(e.status, e.message);
      return fail(500, 'the library could not answer');
    }
  }

  async internal(path, body) {
    if (path === '/internal/init') {
      if (!this.ready) {
        this.schema();
        this.ready = true;
      }
      const [account] = this.rows('select revoked_at from account where id = 1');
      if (account && !account.revoked_at) return fail(409, 'this customer exists');
      this.rows('insert or replace into account (id, hash, name, created_at, revoked_at) values (1, ?, ?, ?, null)',
        body.hash, short(body.name, 200), now());
      return json(200, { ok: true });
    }
    if (path === '/internal/revoke') {
      if (body.erase) {
        await this.ctx.storage.deleteAll();
        this.ready = false;
        this.total = null;
      } else if (this.ready) {
        this.rows('update account set revoked_at = ? where id = 1', now());
      }
      return json(200, { ok: true, erased: Boolean(body.erase) });
    }
    return fail(404, 'no such route');
  }

  async authenticate(request) {
    if (request.headers.get('x-library-via') === 'operator') {
      const denied = await this.member(request);
      if (denied) return denied;
    } else {
      const m = KEY.exec(bearer(request));
      if (!this.ready) return fail(401, 'missing, unknown or revoked library key');
      const [account] = this.rows('select hash, revoked_at from account where id = 1');
      if (!m || !account || account.revoked_at || !sameText(await sha256(m[2]), account.hash)) {
        return fail(401, 'missing, unknown or revoked library key');
      }
    }
    const requests = this.count();
    if (requests > this.limits.dailyRequests) {
      return fail(429, `more than ${this.limits.dailyRequests} requests today; the limit resets at 00:00 UTC`);
    }
    return null;
  }

  // A member of the operator (one user of the AWG hub). The front worker checked
  // the operator's token and set the x-library-* headers itself. The member's
  // library is made the first time it is used; no key opens it, because its
  // hash is of a secret nobody holds. A revoked member stays refused.
  async member(request) {
    const id = request.headers.get('x-library-customer');
    const name = short(request.headers.get('x-library-name'), 200) || `member ${id}`;
    if (!this.ready) {
      this.schema();
      this.ready = true;
    }
    const [account] = this.rows('select revoked_at from account where id = 1');
    if (account?.revoked_at) return fail(401, 'missing, unknown or revoked library key');
    if (!account) {
      this.rows('insert into account (id, hash, name, created_at, revoked_at) values (1, ?, ?, ?, null)',
        await sha256(randomId(48, '0123456789abcdef')), name, now());
      await internal(this.env.DIRECTORY.get(this.env.DIRECTORY.idFromName('directory')), '/internal/add', { id, name });
    }
    return null;
  }

  // The day's requests, counted in memory and written every COUNT_EVERY
  // requests. An object Cloudflare evicts loses at most COUNT_EVERY - 1 of them.
  count() {
    const day = now().slice(0, 10);
    if (this.day !== day) {
      const [stored] = this.rows('select requests from usage_day where day = ?', day);
      this.day = day;
      this.requests = stored?.requests || 0;
    }
    this.requests += 1;
    if (this.requests % COUNT_EVERY === 0) {
      this.rows('insert into usage_day (day, requests) values (?, ?) on conflict(day) do update set requests = excluded.requests',
        day, this.requests);
    }
    return this.requests;
  }

  async route(request, url) {
    const p = url.searchParams;
    const method = request.method;
    if (method === 'POST' && url.pathname === '/v1/items') return await this.save(await readJson(request));
    if (method === 'GET' && url.pathname === '/v1/items') return this.list(p);
    if (url.pathname === '/v1/item' && (method === 'GET' || method === 'DELETE')) return this.one(p, method === 'DELETE');
    if (method === 'GET' && url.pathname === '/v1/search') return this.search(p);
    if (method === 'GET' && url.pathname === '/v1/usage') return this.usage();
    if (method === 'GET' && url.pathname === '/v1/folders') return this.folders();
    if (method === 'POST' && url.pathname === '/v1/move') return this.move(await readJson(request));
    if (method === 'POST' && url.pathname === '/v1/schemas') return this.schemaSave(await readJson(request));
    if (method === 'GET' && url.pathname === '/v1/schemas') return this.schemaList();
    if (url.pathname === '/v1/schema' && (method === 'GET' || method === 'DELETE')) return this.schemaOne(p, method === 'DELETE');
    if (method === 'GET' && url.pathname === '/v1/schema/next') return this.schemaNext(p);
    if (method === 'POST' && url.pathname === '/v1/extractions') return this.extractionSave(await readJson(request));
    if (method === 'GET' && url.pathname === '/v1/extractions') return this.extractionList(p);
    return fail(404, 'no such route');
  }

  // A schema with how far its folder is: texts in the folder, texts with a result.
  schemaRow(name) {
    const [s] = this.rows('select name, folder, description, definition, created_at, updated_at from extract_schema'
      + ' where name = ? collate nocase', name);
    if (!s) return null;
    const [{ total }] = this.rows('select count(*) as total from item where folder = ? collate nocase', s.folder);
    const [{ done }] = this.rows('select count(*) as done from extraction e join item i on i.id = e.item_id'
      + ' where e.schema_name = ? collate nocase and i.folder = ? collate nocase', s.name, s.folder);
    return { ...s, definition: parseKept(s.definition), texts: total, extracted: done, remaining: total - done };
  }

  schemaSave(body) {
    const name = folderName(body.name);
    if (!name) return fail(400, 'name is required');
    const folder = this.spelling(folderName(body.folder));
    if (!folder) return fail(400, 'folder is required');
    if (body.definition === undefined || body.definition === null || body.definition === '') {
      return fail(400, 'definition is required: the fields to pull out of every text');
    }
    const definition = typeof body.definition === 'string' ? body.definition : JSON.stringify(body.definition);
    if (definition.length > SCHEMA_CHARS) return fail(400, `definition over ${SCHEMA_CHARS} characters`);
    const stamp = now();
    this.rows('insert into extract_schema (name, folder, description, definition, created_at, updated_at) values (?, ?, ?, ?, ?, ?)'
      + ' on conflict(name) do update set folder = excluded.folder, description = excluded.description,'
      + ' definition = excluded.definition, updated_at = excluded.updated_at',
    name, folder, short(body.description, 2000), definition, stamp, stamp);
    return json(200, { schema: this.schemaRow(name) });
  }

  schemaList() {
    const names = this.rows('select name from extract_schema order by name collate nocase');
    return json(200, { schemas: names.map((n) => {
      const { definition, ...rest } = this.schemaRow(n.name);
      return rest;
    }) });
  }

  schemaOne(p, remove) {
    const s = this.schemaRow(folderName(p.get('name')));
    if (!s) return fail(404, 'no such schema in this library');
    if (!remove) return json(200, { schema: s });
    this.ctx.storage.transactionSync(() => {
      const [{ freed }] = this.rows('select coalesce(sum(bytes), 0) as freed from extraction where schema_name = ? collate nocase', s.name);
      this.rows('delete from extraction where schema_name = ? collate nocase', s.name);
      this.rows('delete from extract_schema where name = ? collate nocase', s.name);
      if (this.total !== null) this.total -= freed;
    });
    return json(200, { deleted: s.name, results_deleted: s.extracted });
  }

  // The next texts of the schema's folder that have no result yet, oldest
  // first, up to `limit` texts and about `max_chars` characters (always at least one).
  schemaNext(p) {
    const s = this.schemaRow(folderName(p.get('name')));
    if (!s) return fail(404, 'no such schema in this library');
    const limit = Math.min(Math.max(Number(p.get('limit')) || 20, 1), MAX_BATCH);
    const maxChars = Math.min(Math.max(Number(p.get('max_chars')) || 60000, 1000), 200000);
    const rows = this.rows('select i.id, i.url, i.platform, i.kind, i.title, i.author, i.published_at, i.text from item i'
      + ' where i.folder = ? collate nocase and not exists (select 1 from extraction e where e.schema_name = ? collate nocase'
      + ' and e.item_id = i.id) order by i.id limit ?', s.folder, s.name, limit);
    const items = [];
    let chars = 0;
    for (const row of rows) {
      if (items.length && chars + row.text.length > maxChars) break;
      items.push(row);
      chars += row.text.length;
    }
    const { definition, description, name, folder, remaining } = s;
    return json(200, { schema: { name, folder, description, definition }, items, remaining });
  }

  extractionSave(body) {
    const s = this.schemaRow(folderName(body.schema));
    if (!s) return fail(404, 'no such schema in this library');
    const raw = Array.isArray(body.results) ? body.results : [];
    if (!raw.length || raw.length > MAX_BATCH) return fail(400, `results: 1 to ${MAX_BATCH} per request`);
    this.written = 0;
    const saved = [];
    const rejected = [];
    for (const entry of raw) {
      const id = Number(entry?.item_id) || null;
      if (!id) { rejected.push({ item_id: entry?.item_id ?? null, reason: 'item_id is required' }); continue; }
      if (entry.result === undefined || entry.result === null) { rejected.push({ item_id: id, reason: 'result is required' }); continue; }
      const result = typeof entry.result === 'string' ? entry.result : JSON.stringify(entry.result);
      const bytes = utf8(result);
      if (bytes > RESULT_BYTES) { rejected.push({ item_id: id, reason: `result over ${RESULT_BYTES} bytes` }); continue; }
      const outcome = this.ctx.storage.transactionSync(() => {
        const [item] = this.rows('select id from item where id = ?', id);
        if (!item) return { reason: 'no such text in this library' };
        const [old] = this.rows('select bytes from extraction where schema_name = ? collate nocase and item_id = ?', s.name, id);
        const total = this.size();
        if (total - (old?.bytes || 0) + bytes > this.limits.customerBytes) return { reason: 'the library is full' };
        this.rows('insert into extraction (schema_name, item_id, result, bytes, updated_at) values (?, ?, ?, ?, ?)'
          + ' on conflict(schema_name, item_id) do update set result = excluded.result, bytes = excluded.bytes,'
          + ' updated_at = excluded.updated_at', s.name, id, result, bytes, now());
        this.total = total - (old?.bytes || 0) + bytes;
        return { created: !old };
      });
      if (outcome.reason) rejected.push({ item_id: id, reason: outcome.reason });
      else saved.push({ item_id: id, created: outcome.created });
    }
    const after = this.schemaRow(s.name);
    return json(200, { saved, rejected, extracted: after.extracted, remaining: after.remaining, rows_written: this.written });
  }

  extractionList(p) {
    const s = this.schemaRow(folderName(p.get('name')));
    if (!s) return fail(404, 'no such schema in this library');
    const limit = Math.min(Math.max(Number(p.get('limit')) || 25, 1), 100);
    const before = Number(p.get('before')) || Number.MAX_SAFE_INTEGER;
    const rows = this.rows('select e.item_id, i.url, i.author, i.title, i.published_at, e.result, e.updated_at'
      + ' from extraction e join item i on i.id = e.item_id where e.schema_name = ? collate nocase and e.item_id < ?'
      + ' order by e.item_id desc limit ?', s.name, before, limit);
    const results = rows.map((r) => ({ ...r, result: parseKept(r.result) }));
    const next = results.length === limit ? results[results.length - 1].item_id : null;
    return json(200, { schema: s.name, results, extracted: s.extracted, ...(next ? { next_before: next } : {}) });
  }

  async save(body) {
    const raw = Array.isArray(body.items) ? body.items : [];
    if (!raw.length || raw.length > MAX_BATCH) return fail(400, `items: 1 to ${MAX_BATCH} per request`);
    this.written = 0;
    const saved = [];
    const rejected = [];
    for (const entry of raw) {
      const { item, url, reason } = normalize(entry, this.limits.itemBytes);
      if (!item) { rejected.push({ url: url || null, reason }); continue; }
      const { bytes, folder: asked, ...content } = item;
      const folder = this.spelling(asked);
      const sha = await sha256(JSON.stringify(content));
      const outcome = this.ctx.storage.transactionSync(() => {
        const [old] = this.rows('select id, bytes, sha, folder from item where url = ?', item.url);
        // The same text again: nothing to write, nothing billed - unless it is
        // asked into another folder, which moves it (one row).
        if (old && old.sha === sha) {
          if (folder && folder !== old.folder) {
            this.rows('update item set folder = ? where id = ?', folder, old.id);
            return { id: old.id, created: false, unchanged: true, moved: true };
          }
          return { id: old.id, created: false, unchanged: true };
        }
        const total = this.size();
        if (total - (old?.bytes || 0) + item.bytes > this.limits.customerBytes) return { reason: 'the library is full' };
        const stamp = now();
        this.rows(
          'insert into item (url, platform, kind, external_id, title, author, published_at, duration_seconds, language,'
          + ' text, segments, meta, bytes, sha, folder, created_at, updated_at) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)'
          + ' on conflict(url) do update set platform = excluded.platform, kind = excluded.kind, external_id = excluded.external_id,'
          + ' title = excluded.title, author = excluded.author, published_at = excluded.published_at,'
          + ' duration_seconds = excluded.duration_seconds, language = excluded.language, text = excluded.text,'
          + ' segments = excluded.segments, meta = excluded.meta, bytes = excluded.bytes, sha = excluded.sha,'
          + ' folder = coalesce(?, item.folder), updated_at = excluded.updated_at',
          item.url, item.platform, item.kind, item.external_id, item.title, item.author, item.published_at,
          item.duration_seconds, item.language, item.text, item.segments, item.meta, item.bytes, sha, folder || GENERAL,
          stamp, stamp, folder,
        );
        const [{ id }] = this.rows('select id from item where url = ?', item.url);
        this.total = total - (old?.bytes || 0) + item.bytes;
        return { id, created: !old };
      });
      if (outcome.reason) rejected.push({ url: item.url, reason: outcome.reason });
      else {
        saved.push({ id: outcome.id, url: item.url, created: outcome.created, ...(outcome.unchanged ? { unchanged: true } : {}),
          ...(outcome.moved ? { moved: true } : {}) });
      }
    }
    return json(200, { saved, rejected, rows_written: this.written });
  }

  list(p) {
    const limit = Math.min(Math.max(Number(p.get('limit')) || 25, 1), 100);
    const before = Number(p.get('before')) || Number.MAX_SAFE_INTEGER;
    const items = this.rows(
      `select ${LIST_COLUMNS} from item where id < ? and (? is null or platform = ?)`
      + ' and (? is null or lower(author) = lower(?)) and (? is null or folder = ? collate nocase) order by id desc limit ?',
      before, p.get('platform'), p.get('platform'), p.get('author'), p.get('author'),
      folderName(p.get('folder')), folderName(p.get('folder')), limit,
    );
    const next = items.length === limit ? items[items.length - 1].id : null;
    return json(200, { items, ...(next ? { next_before: next } : {}) });
  }

  one(p, remove) {
    const id = Number(p.get('id')) || null;
    const link = p.get('url');
    const platform = p.get('platform');
    const external = p.get('external_id');
    if (!id && !link && !(platform && external)) return fail(400, 'id, url, or platform with external_id is required');
    const [item] = this.rows(
      'select * from item where (? is not null and id = ?) or (? is not null and url = ?)'
      + ' or (? is not null and ? is not null and platform = ? and external_id = ?) order by updated_at desc limit 1',
      id, id, link, link, platform, external, platform, external,
    );
    if (!item) return fail(404, 'no such item');
    if (remove) {
      this.size();
      this.rows('delete from item where id = ?', item.id);
      // Its schema results went with it (trigger item_ad_extraction): count anew.
      this.total = null;
      return json(200, { deleted: { id: item.id, url: item.url } });
    }
    const { bytes, sha, segments, meta, ...rest } = item;
    return json(200, { ...rest, segments: segments ? JSON.parse(segments) : [], meta: meta ? JSON.parse(meta) : {} });
  }

  search(p) {
    const match = ftsQuery(p.get('q'));
    if (!match) return fail(400, 'q needs at least one word');
    const limit = Math.min(Math.max(Number(p.get('limit')) || 10, 1), 50);
    const results = this.rows(
      'select i.id, i.url, i.folder, i.platform, i.kind, i.title, i.author, i.published_at,'
      + " snippet(item_fts, 2, '«', '»', ' … ', 24) as snippet from item_fts join item i on i.id = item_fts.rowid"
      + ' where item_fts match ? and (? is null or i.platform = ?) and (? is null or i.folder = ? collate nocase)'
      + ' order by bm25(item_fts) limit ?',
      match, p.get('platform'), p.get('platform'), folderName(p.get('folder')), folderName(p.get('folder')), limit,
    );
    return json(200, { query: p.get('q'), results });
  }

  // A folder that exists keeps the spelling it has: "immobilien" is "Immobilien".
  spelling(name) {
    if (!name) return name;
    const [found] = this.rows('select folder from item where folder = ? collate nocase limit 1', name);
    return found?.folder || name;
  }

  // The folders there are, with how many texts each holds.
  folders() {
    const folders = this.rows('select folder as name, count(*) as items, coalesce(sum(bytes), 0) as bytes,'
      + ' max(updated_at) as last_change from item group by folder collate nocase order by folder collate nocase');
    return json(200, { folders });
  }

  // Moves one text (by id or link) into a folder, or renames a whole folder
  // ({from, to}); a folder exists while it holds texts.
  move(body) {
    const to = this.spelling(folderName(body.to ?? body.folder));
    if (!to) return fail(400, 'the folder to move into is required');
    if (body.from !== undefined) {
      const from = folderName(body.from);
      if (!from) return fail(400, 'from must name a folder');
      this.written = 0;
      this.rows('update item set folder = ? where folder = ? collate nocase', to, from);
      return json(200, { renamed: from, to, rows_written: this.written });
    }
    const id = Number(body.id) || null;
    const link = body.url ? String(body.url) : null;
    if (!id && !link) return fail(400, 'id or url of the text, or from (a folder to rename), is required');
    const [item] = this.rows('select id, url from item where (? is not null and id = ?) or (? is not null and url = ?) limit 1',
      id, id, link, link);
    if (!item) return fail(404, 'no such text in this library');
    this.rows('update item set folder = ? where id = ?', to, item.id);
    return json(200, { id: item.id, url: item.url, folder: to });
  }

  usage() {
    const [{ items, bytes }] = this.rows('select count(*) as items, coalesce(sum(bytes), 0) as bytes from item');
    const [today] = this.rows('select requests from usage_day where day = ?', now().slice(0, 10));
    return json(200, {
      items, bytes, database_bytes: this.ctx.storage.sql.databaseSize ?? null, requests_today: this.day === now().slice(0, 10) ? this.requests : (today?.requests || 0),
      limits: { customer_bytes: this.limits.customerBytes, item_bytes: this.limits.itemBytes, daily_requests: this.limits.dailyRequests },
    });
  }
}

// Who the customers are, for the operator: names and dates, never keys.
export class Directory {
  constructor(ctx) {
    this.ctx = ctx;
    ctx.storage.sql.exec('create table if not exists customer (id text primary key, name text, created_at text not null, revoked_at text)');
  }

  async fetch(request) {
    const { pathname } = new URL(request.url);
    const sql = this.ctx.storage.sql;
    if (pathname === '/internal/list') {
      return json(200, { customers: sql.exec('select * from customer order by created_at').toArray() });
    }
    const body = await readJson(request);
    if (pathname === '/internal/add') {
      sql.exec('insert into customer (id, name, created_at) values (?, ?, ?)'
        + ' on conflict(id) do update set name = excluded.name, revoked_at = null', body.id, short(body.name, 200), now());
      return json(200, { ok: true });
    }
    if (pathname === '/internal/revoke') {
      sql.exec('update customer set revoked_at = ? where id = ?', now(), body.id);
      return json(200, { ok: true });
    }
    return fail(404, 'no such route');
  }
}

const internal = (stub, path, body) => stub.fetch(new Request(`https://library.internal${path}`, {
  method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(body || {}),
}));

async function admin(request, env, url) {
  // The worker knows only the admin token's SHA-256, never the token.
  const expected = String(env.ADMIN_TOKEN_SHA256 || '').toLowerCase();
  if (!/^[a-f0-9]{64}$/.test(expected)) return fail(503, 'ADMIN_TOKEN_SHA256 is not set on this worker');
  const given = bearer(request);
  if (given.length < 32 || !sameText(await sha256(given), expected)) return fail(401, 'admin token required');
  const directory = env.DIRECTORY.get(env.DIRECTORY.idFromName('directory'));
  if (url.pathname === '/v1/admin/customers' && request.method === 'POST') {
    const { name } = await readJson(request);
    if (!short(name, 200)) return fail(400, 'name is required');
    const id = randomId(16, 'abcdefghijklmnopqrstuvwxyz0123456789');
    const secret = randomId(48, '0123456789abcdef');
    const library = env.LIBRARY.get(env.LIBRARY.idFromName(id));
    const made = await internal(library, '/internal/init', { hash: await sha256(secret), name });
    if (!made.ok) return made;
    await internal(directory, '/internal/add', { id, name });
    return json(201, { customer: id, name: short(name, 200), key: `sml_${id}_${secret}`, note: 'The key is shown only now.' });
  }
  if (url.pathname === '/v1/admin/customers' && request.method === 'GET') {
    return directory.fetch(new Request('https://library.internal/internal/list'));
  }
  const m = /^\/v1\/admin\/customers\/([a-z0-9]{16})$/.exec(url.pathname);
  if (m && request.method === 'DELETE') {
    const erase = url.searchParams.get('erase') === 'true';
    await internal(env.LIBRARY.get(env.LIBRARY.idFromName(m[1])), '/internal/revoke', { erase });
    await internal(directory, '/internal/revoke', { id: m[1] });
    return json(200, { customer: m[1], revoked: true, erased: erase });
  }
  return fail(404, 'no such route');
}

// The operator (the AWG hub) keeps one library per user of its own with one
// token: it names the user's library in x-library-customer. The worker knows
// only the token's SHA-256. Without OPERATOR_TOKEN_SHA256 there is no operator.
async function operator(request, env, token) {
  const expected = String(env.OPERATOR_TOKEN_SHA256 || '').toLowerCase();
  if (!/^[a-f0-9]{64}$/.test(expected) || token.length < 32 || !sameText(await sha256(token), expected)) {
    return fail(401, 'missing, unknown or revoked library key');
  }
  const id = request.headers.get('x-library-customer') || '';
  if (!/^[a-z0-9]{16}$/.test(id)) return fail(400, 'x-library-customer must name a library: 16 letters and digits');
  request.headers.set('x-library-via', 'operator');
  return env.LIBRARY.get(env.LIBRARY.idFromName(id)).fetch(request);
}

// Video downloads with a one-hour link. The SMTranscript worker uploads a file
// in parts with the upload token (the worker knows only its SHA-256, secret
// UPLOAD_TOKEN_SHA256); anyone with the link can fetch it until it expires one
// hour after the upload completed. Then the Downloads object deletes it from R2,
// and nothing can extend that hour.
export const PART_SIZE = 64 * 1024 * 1024;
export const MAX_DOWNLOAD_BYTES = 4 * 1024 ** 3;
export const MAX_STORED_BYTES = 100 * 1024 ** 3;
export const LINK_MS = 3600 * 1000;
export const UPLOAD_MS = 3600 * 1000;
// A delete R2 refused is tried again after a minute; rows of gone files are
// kept 30 days, so a late click hears "expired" (410) rather than "unknown".
const RETRY_MS = 60 * 1000;
const KEEP_GONE_MS = 30 * 24 * 3600 * 1000;
const HEX = '0123456789abcdef';

// A file name safe in a URL path and a Content-Disposition header: ASCII
// letters, digits, dot, dash and underscore, the extension kept, up to 150 characters.
export function downloadName(raw) {
  const name = String(raw ?? '').normalize('NFKD').replace(/[̀-ͯ]/g, '').split(/[/\\]/).pop();
  const dot = name.lastIndexOf('.');
  const ext = dot > 0 && /^[A-Za-z0-9]{1,10}$/.test(name.slice(dot + 1)) ? name.slice(dot + 1) : '';
  const stem = (ext ? name.slice(0, dot) : name).replace(/[^A-Za-z0-9._-]+/g, '_').replace(/_+/g, '_')
    .replace(/^[._-]+|[._-]+$/g, '');
  const room = 150 - (ext ? ext.length + 1 : 0);
  const base = stem.slice(0, room).replace(/[._-]+$/, '') || 'download';
  return ext ? `${base}.${ext}` : base;
}

// One byte range of a `Range: bytes=...` header as R2 takes it, null to send
// the whole file (no header, or one this worker does not read), or
// 'unsatisfiable' for a range outside the file.
export function byteRange(header, size) {
  const m = /^bytes=(\d*)-(\d*)$/.exec(String(header || '').trim());
  if (!m || (m[1] === '' && m[2] === '')) return null;
  if (m[1] === '') {
    const length = Math.min(Number(m[2]), size);
    return length > 0 ? { offset: size - length, length } : 'unsatisfiable';
  }
  const start = Number(m[1]);
  const end = m[2] === '' ? size - 1 : Math.min(Number(m[2]), size - 1);
  if (start >= size || end < start) return 'unsatisfiable';
  return { offset: start, length: end - start + 1 };
}

const disposition = (filename) => `attachment; filename="${filename}"`;
const partsOf = (size) => Math.ceil(size / PART_SIZE);

const DOWNLOAD_SCHEMA = [
  'create table if not exists download (id text primary key, key text not null, upload_id text, filename text not null,'
    + ' mime text not null, size integer not null, title text, source_url text, created_at integer not null,'
    + ' completed_at integer, expires_at integer, state text not null)',
  'create table if not exists download_part (id text not null, n integer not null, etag text not null, primary key (id, n))',
  'create index if not exists download_state on download (state)',
];

// The rows of every download, and the alarm that deletes each one when its
// hour is over. One object for all (idFromName('downloads')), so the
// storage ceiling is checked and reserved in one place.
export class Downloads {
  constructor(ctx, env) {
    this.ctx = ctx;
    this.env = env;
    for (const statement of DOWNLOAD_SCHEMA) ctx.storage.sql.exec(statement);
  }

  rows(query, ...bindings) {
    return this.ctx.storage.sql.exec(query, ...bindings.map((b) => (b === undefined ? null : b))).toArray();
  }

  row(id) {
    return this.rows('select * from download where id = ?', id)[0] || null;
  }

  async fetch(request) {
    try {
      const { pathname } = new URL(request.url);
      const body = await readJson(request);
      if (pathname === '/internal/create') return await this.create(body);
      if (pathname === '/internal/get') {
        const row = this.row(body.id);
        return row ? json(200, row) : fail(404, 'no such download');
      }
      if (pathname === '/internal/part') return this.part(body);
      if (pathname === '/internal/complete') return await this.complete(body);
      if (pathname === '/internal/abort') return await this.abort(body);
      return fail(404, 'no such route');
    } catch (e) {
      if (e instanceof Refused) return fail(e.status, e.message);
      return fail(500, 'the downloads could not answer');
    }
  }

  async create(body) {
    const size = Number(body.size);
    if (!Number.isSafeInteger(size) || size < 1) return fail(400, 'size must be the file\'s size in bytes');
    if (size > MAX_DOWNLOAD_BYTES) return fail(413, `a file is at most ${MAX_DOWNLOAD_BYTES} bytes`);
    const filename = downloadName(body.filename);
    const mime = /^[\w.+-]+\/[\w.+-]+$/.test(body.mime || '') ? body.mime : 'application/octet-stream';
    const id = randomId(32, HEX);
    const key = `downloads/${id}/${filename}`;
    // Checked and reserved with no await between: no other request can slip in.
    const [{ stored }] = this.rows("select coalesce(sum(size), 0) as stored from download where state in ('uploading', 'live')");
    if (stored + size > MAX_STORED_BYTES) return fail(507, 'the download storage is full; files leave it within the hour');
    this.rows('insert into download (id, key, filename, mime, size, title, source_url, created_at, state)'
      + " values (?, ?, ?, ?, ?, ?, ?, ?, 'uploading')", id, key, filename, mime, size, short(body.title, 500),
    short(body.source_url, 2048), Date.now());
    let upload;
    try {
      upload = await this.env.DOWNLOADS.createMultipartUpload(key, { httpMetadata: { contentType: mime, contentDisposition: disposition(filename) } });
    } catch (e) {
      this.rows("update download set state = 'aborted' where id = ?", id);
      throw e;
    }
    this.rows('update download set upload_id = ? where id = ?', upload.uploadId, id);
    await this.arm();
    return json(200, { id, part_size: PART_SIZE, parts: partsOf(size), filename });
  }

  part(body) {
    const row = this.row(body.id);
    if (!row) return fail(404, 'no such download');
    if (row.state !== 'uploading') return fail(409, `this download is ${row.state}`);
    this.rows('insert or replace into download_part (id, n, etag) values (?, ?, ?)', row.id, body.n, String(body.etag));
    const [{ received }] = this.rows('select count(*) as received from download_part where id = ?', row.id);
    return json(200, { part: body.n, received, parts: partsOf(row.size) });
  }

  answer(row, origin) {
    return json(200, {
      url: `${origin}/d/${row.id}/${encodeURIComponent(row.filename)}`, expires_at: new Date(row.expires_at).toISOString(),
      filename: row.filename, size: row.size, title: row.title,
    });
  }

  async complete(body) {
    const row = this.row(body.id);
    if (!row) return fail(404, 'no such download');
    // A retried complete gets the same link again.
    if (row.state === 'live' && row.expires_at > Date.now()) return this.answer(row, body.origin);
    if (row.state !== 'uploading') return fail(409, `this download is ${row.state === 'live' ? 'expired' : row.state}`);
    if (row.created_at + UPLOAD_MS <= Date.now()) return fail(410, 'the upload took over an hour and was dropped');
    const parts = this.rows('select n as partNumber, etag from download_part where id = ? order by n', row.id);
    const missing = [];
    for (let n = 1; n <= partsOf(row.size); n += 1) if (!parts.some((p) => p.partNumber === n)) missing.push(n);
    if (missing.length) return fail(409, `parts missing: ${missing.slice(0, 20).join(', ')}`);
    const object = await this.env.DOWNLOADS.resumeMultipartUpload(row.key, row.upload_id).complete(parts);
    if (object.size !== row.size) {
      await this.env.DOWNLOADS.delete(row.key);
      this.rows("update download set state = 'aborted' where id = ?", row.id);
      return fail(409, `the parts make ${object.size} bytes, not the ${row.size} announced; the upload was dropped`);
    }
    const done = Date.now();
    this.rows("update download set state = 'live', completed_at = ?, expires_at = ? where id = ?", done, done + LINK_MS, row.id);
    this.rows('delete from download_part where id = ?', row.id);
    await this.arm();
    return this.answer(this.row(row.id), body.origin);
  }

  async abort(body) {
    const row = this.row(body.id);
    if (!row) return fail(404, 'no such download');
    if (row.state === 'aborted') return json(200, { aborted: true });
    if (row.state !== 'uploading') return fail(409, `this download is ${row.state}; only an upload can be aborted`);
    await this.drop(row);
    await this.arm();
    return json(200, { aborted: true });
  }

  // An unfinished upload goes: its row first, so no part or complete lands after.
  async drop(row) {
    this.rows("update download set state = 'aborted' where id = ?", row.id);
    this.rows('delete from download_part where id = ?', row.id);
    if (!row.upload_id) return;
    try {
      await this.env.DOWNLOADS.resumeMultipartUpload(row.key, row.upload_id).abort();
    } catch {
      // Gone already, or R2 refused: the bucket's lifecycle rule ends it within a day.
    }
  }

  async alarm() {
    const time = Date.now();
    for (const row of this.rows("select id, key from download where state = 'live' and expires_at <= ?", time)) {
      try {
        await this.env.DOWNLOADS.delete(row.key);
        this.rows("update download set state = 'deleted' where id = ?", row.id);
      } catch {
        // Stays due; arm() tries again in a minute.
      }
    }
    for (const row of this.rows("select * from download where state = 'uploading' and created_at <= ?", time - UPLOAD_MS)) {
      await this.drop(row);
    }
    this.rows("delete from download where state in ('deleted', 'aborted') and created_at <= ?", time - KEEP_GONE_MS);
    await this.arm();
  }

  // The alarm is set for the next file to delete or upload to drop.
  async arm() {
    const [{ next }] = this.rows("select min(due) as next from (select expires_at as due from download where state = 'live'"
      + " union all select created_at + ? as due from download where state = 'uploading')", UPLOAD_MS);
    if (next === null) await this.ctx.storage.deleteAlarm();
    else await this.ctx.storage.setAlarm(next <= Date.now() ? Date.now() + RETRY_MS : next);
  }
}

async function downloads(request, env, url) {
  const expected = String(env.UPLOAD_TOKEN_SHA256 || '').toLowerCase();
  if (!/^[a-f0-9]{64}$/.test(expected)) return fail(503, 'UPLOAD_TOKEN_SHA256 is not set on this worker');
  const given = bearer(request);
  if (given.length < 32 || !sameText(await sha256(given), expected)) return fail(401, 'upload token required');
  const index = env.DOWNLOAD_INDEX.get(env.DOWNLOAD_INDEX.idFromName('downloads'));
  if (url.pathname === '/v1/downloads' && request.method === 'POST') return internal(index, '/internal/create', await readJson(request));
  const m = /^\/v1\/downloads\/([a-f0-9]{32})\/(?:parts\/(\d{1,5})|(complete|abort))$/.exec(url.pathname);
  if (m && m[2] && request.method === 'PUT') return uploadPart(request, env, index, m[1], Number(m[2]));
  if (m && m[3] && request.method === 'POST') return internal(index, `/internal/${m[3]}`, { id: m[1], origin: url.origin });
  return fail(404, 'no such route');
}

// The bytes go from the request straight to R2; the Downloads object only
// keeps which parts arrived.
async function uploadPart(request, env, index, id, n) {
  const found = await internal(index, '/internal/get', { id });
  if (!found.ok) return found;
  const row = await found.json();
  if (row.state !== 'uploading') return fail(409, `this download is ${row.state}`);
  if (row.created_at + UPLOAD_MS <= Date.now()) return fail(410, 'the upload took over an hour and was dropped');
  const parts = partsOf(row.size);
  if (n < 1 || n > parts) return fail(400, `part must be 1 to ${parts}`);
  // R2 joins only equal parts: every part but the last is exactly PART_SIZE.
  const expected = n < parts ? PART_SIZE : row.size - (parts - 1) * PART_SIZE;
  const length = request.headers.get('content-length');
  if (length === null) return fail(411, 'content-length is required');
  if (Number(length) !== expected) return fail(400, `part ${n} must be exactly ${expected} bytes`);
  const uploaded = await env.DOWNLOADS.resumeMultipartUpload(row.key, row.upload_id).uploadPart(n, request.body);
  return internal(index, '/internal/part', { id, n, etag: uploaded.etag });
}

// The public link: no key, the 128-bit id is the secret. Streams from R2 and
// answers byte ranges; 410 once the hour is over.
async function serve(request, env, id, name) {
  if (request.method !== 'GET' && request.method !== 'HEAD') return fail(405, 'GET or HEAD only');
  const found = await internal(env.DOWNLOAD_INDEX.get(env.DOWNLOAD_INDEX.idFromName('downloads')), '/internal/get', { id });
  if (found.status === 404) return fail(404, 'no such download');
  if (!found.ok) return found;
  const row = await found.json();
  if (row.filename !== name) return fail(404, 'no such download');
  const gone = fail(410, 'this download expired: a link lasts one hour');
  if (row.state === 'deleted' || (row.state === 'live' && row.expires_at <= Date.now())) return gone;
  if (row.state !== 'live') return fail(404, 'no such download');
  const headers = {
    'content-type': row.mime, 'content-disposition': disposition(row.filename), 'accept-ranges': 'bytes', 'cache-control': 'no-store',
  };
  if (request.method === 'HEAD') {
    if (!(await env.DOWNLOADS.head(row.key))) return gone;
    return new Response(null, { status: 200, headers: { ...headers, 'content-length': String(row.size) } });
  }
  const range = byteRange(request.headers.get('range'), row.size);
  if (range === 'unsatisfiable') {
    return new Response(null, { status: 416, headers: { ...headers, 'content-range': `bytes */${row.size}` } });
  }
  const object = await env.DOWNLOADS.get(row.key, range ? { range } : {});
  if (!object) return gone;
  if (!range) return new Response(object.body, { status: 200, headers: { ...headers, 'content-length': String(row.size) } });
  return new Response(object.body, {
    status: 206,
    headers: {
      ...headers, 'content-length': String(range.length),
      'content-range': `bytes ${range.offset}-${range.offset + range.length - 1}/${row.size}`,
    },
  });
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    try {
      if (url.pathname === '/healthz') return json(200, { ok: true });
      if (url.pathname.startsWith('/v1/admin/')) return await admin(request, env, url);
      if (url.pathname === '/v1/downloads' || url.pathname.startsWith('/v1/downloads/')) return await downloads(request, env, url);
      const link = /^\/d\/([a-f0-9]{32})\/([^/]+)$/.exec(url.pathname);
      if (link) {
        let name;
        try {
          name = decodeURIComponent(link[2]);
        } catch {
          return fail(404, 'no such download');
        }
        return await serve(request, env, link[1], name);
      }
      if (url.pathname.startsWith('/v1/')) {
        const token = bearer(request);
        const m = KEY.exec(token);
        // Only this worker says how a request was let in: a caller's own
        // x-library-via header is dropped.
        const forwarded = new Request(request);
        forwarded.headers.delete('x-library-via');
        // The key names the customer's database; that database checks the secret.
        if (m) return await env.LIBRARY.get(env.LIBRARY.idFromName(m[1])).fetch(forwarded);
        return await operator(forwarded, env, token);
      }
      return fail(404, 'no such route');
    } catch (e) {
      if (e instanceof Refused) return fail(e.status, e.message);
      return fail(500, 'the library could not answer');
    }
  },
};
