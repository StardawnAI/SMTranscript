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
  'create trigger if not exists item_au after update on item begin'
    + " insert into item_fts(item_fts, rowid, title, author, text) values ('delete', old.id, old.title, old.author, old.text);"
    + ' insert into item_fts(rowid, title, author, text) values (new.id, new.title, new.author, new.text); end',
  'create table if not exists usage_day (day text primary key, requests integer not null)',
];

const LIST_COLUMNS = 'id, url, platform, kind, external_id, title, author, published_at, duration_seconds, language,'
  + ' length(text) as characters, created_at, updated_at';

// One customer's database.
export class Library {
  constructor(ctx, env) {
    this.ctx = ctx;
    this.env = env;
    this.limits = limits(env);
    this.schema();
  }

  schema() {
    for (const statement of SCHEMA) this.ctx.storage.sql.exec(statement);
  }

  rows(query, ...bindings) {
    return this.ctx.storage.sql.exec(query, ...bindings.map((b) => (b === undefined ? null : b))).toArray();
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
      const [account] = this.rows('select revoked_at from account where id = 1');
      if (account && !account.revoked_at) return fail(409, 'this customer exists');
      this.rows('insert or replace into account (id, hash, name, created_at, revoked_at) values (1, ?, ?, ?, null)',
        body.hash, short(body.name, 200), now());
      return json(200, { ok: true });
    }
    if (path === '/internal/revoke') {
      if (body.erase) {
        await this.ctx.storage.deleteAll();
        this.schema();
      } else {
        this.rows('update account set revoked_at = ? where id = 1', now());
      }
      return json(200, { ok: true, erased: Boolean(body.erase) });
    }
    return fail(404, 'no such route');
  }

  async authenticate(request) {
    const m = KEY.exec(bearer(request));
    const [account] = this.rows('select hash, revoked_at from account where id = 1');
    if (!m || !account || account.revoked_at || !sameText(await sha256(m[2]), account.hash)) {
      return fail(401, 'missing, unknown or revoked library key');
    }
    const day = now().slice(0, 10);
    this.rows('insert into usage_day (day, requests) values (?, 1) on conflict(day) do update set requests = requests + 1', day);
    const [{ requests }] = this.rows('select requests from usage_day where day = ?', day);
    if (requests > this.limits.dailyRequests) {
      return fail(429, `more than ${this.limits.dailyRequests} requests today; the limit resets at 00:00 UTC`);
    }
    return null;
  }

  async route(request, url) {
    const p = url.searchParams;
    const method = request.method;
    if (method === 'POST' && url.pathname === '/v1/items') return this.save(await readJson(request));
    if (method === 'GET' && url.pathname === '/v1/items') return this.list(p);
    if (url.pathname === '/v1/item' && (method === 'GET' || method === 'DELETE')) return this.one(p, method === 'DELETE');
    if (method === 'GET' && url.pathname === '/v1/search') return this.search(p);
    if (method === 'GET' && url.pathname === '/v1/usage') return this.usage();
    return fail(404, 'no such route');
  }

  save(body) {
    const raw = Array.isArray(body.items) ? body.items : [];
    if (!raw.length || raw.length > MAX_BATCH) return fail(400, `items: 1 to ${MAX_BATCH} per request`);
    const saved = [];
    const rejected = [];
    for (const entry of raw) {
      const { item, url, reason } = normalize(entry, this.limits.itemBytes);
      if (!item) { rejected.push({ url: url || null, reason }); continue; }
      const outcome = this.ctx.storage.transactionSync(() => {
        const [{ total }] = this.rows('select coalesce(sum(bytes), 0) as total from item');
        const [old] = this.rows('select id, bytes from item where url = ?', item.url);
        if (total - (old?.bytes || 0) + item.bytes > this.limits.customerBytes) return { reason: 'the library is full' };
        const stamp = now();
        this.rows(
          'insert into item (url, platform, kind, external_id, title, author, published_at, duration_seconds, language,'
          + ' text, segments, meta, bytes, created_at, updated_at) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)'
          + ' on conflict(url) do update set platform = excluded.platform, kind = excluded.kind, external_id = excluded.external_id,'
          + ' title = excluded.title, author = excluded.author, published_at = excluded.published_at,'
          + ' duration_seconds = excluded.duration_seconds, language = excluded.language, text = excluded.text,'
          + ' segments = excluded.segments, meta = excluded.meta, bytes = excluded.bytes, updated_at = excluded.updated_at',
          item.url, item.platform, item.kind, item.external_id, item.title, item.author, item.published_at,
          item.duration_seconds, item.language, item.text, item.segments, item.meta, item.bytes, stamp, stamp,
        );
        const [{ id }] = this.rows('select id from item where url = ?', item.url);
        return { id, created: !old };
      });
      if (outcome.reason) rejected.push({ url: item.url, reason: outcome.reason });
      else saved.push({ id: outcome.id, url: item.url, created: outcome.created });
    }
    return json(200, { saved, rejected });
  }

  list(p) {
    const limit = Math.min(Math.max(Number(p.get('limit')) || 25, 1), 100);
    const before = Number(p.get('before')) || Number.MAX_SAFE_INTEGER;
    const items = this.rows(
      `select ${LIST_COLUMNS} from item where id < ? and (? is null or platform = ?)`
      + ' and (? is null or lower(author) = lower(?)) order by id desc limit ?',
      before, p.get('platform'), p.get('platform'), p.get('author'), p.get('author'), limit,
    );
    const next = items.length === limit ? items[items.length - 1].id : null;
    return json(200, { items, ...(next ? { next_before: next } : {}) });
  }

  one(p, remove) {
    const id = Number(p.get('id')) || null;
    const link = p.get('url');
    if (!id && !link) return fail(400, 'id or url is required');
    const [item] = this.rows('select * from item where (? is not null and id = ?) or (? is not null and url = ?)', id, id, link, link);
    if (!item) return fail(404, 'no such item');
    if (remove) {
      this.rows('delete from item where id = ?', item.id);
      return json(200, { deleted: { id: item.id, url: item.url } });
    }
    const { bytes, segments, meta, ...rest } = item;
    return json(200, { ...rest, segments: segments ? JSON.parse(segments) : [], meta: meta ? JSON.parse(meta) : {} });
  }

  search(p) {
    const match = ftsQuery(p.get('q'));
    if (!match) return fail(400, 'q needs at least one word');
    const limit = Math.min(Math.max(Number(p.get('limit')) || 10, 1), 50);
    const results = this.rows(
      'select i.id, i.url, i.platform, i.kind, i.title, i.author, i.published_at,'
      + " snippet(item_fts, 2, '«', '»', ' … ', 24) as snippet from item_fts join item i on i.id = item_fts.rowid"
      + ' where item_fts match ? and (? is null or i.platform = ?) order by bm25(item_fts) limit ?',
      match, p.get('platform'), p.get('platform'), limit,
    );
    return json(200, { query: p.get('q'), results });
  }

  usage() {
    const [{ items, bytes }] = this.rows('select count(*) as items, coalesce(sum(bytes), 0) as bytes from item');
    const [today] = this.rows('select requests from usage_day where day = ?', now().slice(0, 10));
    return json(200, {
      items, bytes, requests_today: today?.requests || 0,
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
      sql.exec('insert into customer (id, name, created_at) values (?, ?, ?)', body.id, short(body.name, 200), now());
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

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    try {
      if (url.pathname === '/healthz') return json(200, { ok: true });
      if (url.pathname.startsWith('/v1/admin/')) return await admin(request, env, url);
      if (url.pathname.startsWith('/v1/')) {
        const m = KEY.exec(bearer(request));
        if (!m) return fail(401, 'missing, unknown or revoked library key');
        // The key names the customer's database; that database checks the secret.
        return await env.LIBRARY.get(env.LIBRARY.idFromName(m[1])).fetch(request);
      }
      return fail(404, 'no such route');
    } catch (e) {
      if (e instanceof Refused) return fail(e.status, e.message);
      return fail(500, 'the library could not answer');
    }
  },
};
