// node:sqlite stands in for a Durable Object's SQLite, so schema, FTS5 and
// triggers run for real; only the Cloudflare plumbing around them is faked.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { DatabaseSync } from 'node:sqlite';
import worker, { Library, ftsQuery, normalize } from './worker.mjs';
import { cloudflareEnv, storage } from './fake-cloudflare.mjs';

function stack(vars = {}) {
  const env = cloudflareEnv(vars);
  const call = async (method, path, { key, body } = {}) => {
    const r = await worker.fetch(new Request(`https://library.test${path}`, {
      method,
      headers: { ...(key ? { authorization: `Bearer ${key}` } : {}), 'content-type': 'application/json' },
      ...(body !== undefined ? { body: JSON.stringify(body) } : {}),
    }), env);
    return { status: r.status, data: await r.json() };
  };
  const customer = async (name) => (await call('POST', '/v1/admin/customers', { key: env.ADMIN_TOKEN, body: { name } })).data;
  return { env, call, customer };
}

const reel = (n, text) => ({
  url: `https://www.instagram.com/reel/R${n}/`, platform: 'instagram', kind: 'video', external_id: `R${n}`,
  author: 'immo.kanal', title: null, language: 'de', duration_seconds: 31,
  text, segments: [{ start_ms: 0, end_ms: 4000, text }], meta: { source: 'asr', model: 'gpt-4o-mini-transcribe' },
});

test('without a key, with a made-up key or with the wrong secret nothing is reachable', async () => {
  const { call, customer } = stack();
  const { key } = await customer('Kunde A');
  assert.equal((await call('GET', '/v1/items')).status, 401);
  assert.equal((await call('GET', '/v1/items', { key: 'sml_abc' })).status, 401);
  const wrongSecret = key.replace(/_[a-f0-9]{48}$/, `_${'0'.repeat(48)}`);
  assert.equal((await call('GET', '/v1/items', { key: wrongSecret })).status, 401);
  assert.equal((await call('GET', '/v1/items', { key })).status, 200);
});

test('admin routes need the admin token and refuse to run without one', async () => {
  const { call } = stack();
  assert.equal((await call('POST', '/v1/admin/customers', { key: 'b'.repeat(40), body: { name: 'x' } })).status, 401);
  const unset = stack({ ADMIN_TOKEN: '' });
  assert.equal((await unset.call('GET', '/v1/admin/customers', { key: '' })).status, 503);
});

test('one customer never sees another customer\'s texts', async () => {
  const { call, customer } = stack();
  const a = await customer('Kunde A');
  const b = await customer('Kunde B');
  await call('POST', '/v1/items', { key: a.key, body: { items: [reel(1, 'Die Grunderwerbsteuer in Bayern')] } });
  assert.equal((await call('GET', '/v1/items', { key: b.key })).data.items.length, 0);
  assert.equal((await call('GET', '/v1/search?q=Grunderwerbsteuer', { key: b.key })).data.results.length, 0);
  assert.equal((await call('GET', '/v1/search?q=Grunderwerbsteuer', { key: a.key })).data.results.length, 1);
});

test('saving the same link twice keeps one item with the newer text', async () => {
  const { call, customer } = stack();
  const { key } = await customer('Kunde A');
  const first = await call('POST', '/v1/items', { key, body: { items: [reel(1, 'erste Fassung')] } });
  const second = await call('POST', '/v1/items', { key, body: { items: [reel(1, 'zweite Fassung')] } });
  assert.equal(first.data.saved[0].created, true);
  assert.equal(second.data.saved[0].created, false);
  assert.equal(second.data.saved[0].id, first.data.saved[0].id);
  const { data } = await call('GET', `/v1/item?id=${first.data.saved[0].id}`, { key });
  assert.equal(data.text, 'zweite Fassung');
  assert.deepEqual(data.segments, [{ start_ms: 0, end_ms: 4000, text: 'zweite Fassung' }]);
  assert.equal((await call('GET', '/v1/search?q=erste', { key })).data.results.length, 0, 'the old text left the index');
  assert.equal((await call('GET', '/v1/usage', { key })).data.items, 1);
});

test('search finds word beginnings, ignores accents and cannot be fed FTS syntax', async () => {
  const { call, customer } = stack();
  const { key } = await customer('Kunde A');
  await call('POST', '/v1/items', { key, body: { items: [reel(1, 'Rendite bei Mehrfamilienhäusern in Köln'), reel(2, 'Kaufnebenkosten erklärt')] } });
  const hit = await call('GET', '/v1/search?q=mehrfamilien koln', { key });
  assert.equal(hit.data.results.length, 1);
  assert.match(hit.data.results[0].snippet, /«Mehrfamilienhäusern»/);
  for (const q of ['" OR *', 'NEAR(a b)', 'text:x AND', '***']) {
    const r = await call('GET', `/v1/search?q=${encodeURIComponent(q)}`, { key });
    assert.ok([200, 400].includes(r.status), `${q} answered ${r.status}`);
  }
  assert.equal(ftsQuery('a" OR b'), '"a"* "OR"* "b"*');
});

test('list pages through newest first; get and delete work by id or link', async () => {
  const { call, customer } = stack();
  const { key } = await customer('Kunde A');
  await call('POST', '/v1/items', { key, body: { items: [1, 2, 3].map((n) => reel(n, `Text ${n}`)) } });
  const page = await call('GET', '/v1/items?limit=2', { key });
  assert.deepEqual(page.data.items.map((i) => i.external_id), ['R3', 'R2']);
  assert.equal(page.data.items[0].characters, 6);
  assert.equal(page.data.items[0].text, undefined, 'a listing carries no text');
  const rest = await call('GET', `/v1/items?limit=2&before=${page.data.next_before}`, { key });
  assert.deepEqual(rest.data.items.map((i) => i.external_id), ['R1']);
  const url = encodeURIComponent(reel(2, '').url);
  assert.equal((await call('GET', `/v1/item?url=${url}`, { key })).data.text, 'Text 2');
  assert.equal((await call('DELETE', `/v1/item?url=${url}`, { key })).status, 200);
  assert.equal((await call('GET', `/v1/item?url=${url}`, { key })).status, 404);
  assert.equal((await call('GET', '/v1/search?q=Text', { key })).data.results.length, 2);
});

test('the ceilings hold: item size, library size, batch size, requests a day', async () => {
  const { call, customer } = stack({ MAX_ITEM_BYTES: '500', MAX_CUSTOMER_BYTES: '900', MAX_DAILY_REQUESTS: '6' });
  const { key } = await customer('Kunde A');
  const big = await call('POST', '/v1/items', { key, body: { items: [reel(1, 'x'.repeat(600))] } });
  assert.match(big.data.rejected[0].reason, /over 500 bytes/);
  const fill = await call('POST', '/v1/items', { key, body: { items: [reel(2, 'y'.repeat(150)), reel(3, 'z'.repeat(150)), reel(4, 'w'.repeat(150))] } });
  assert.equal(fill.data.saved.length, 2);
  assert.equal(fill.data.rejected[0].reason, 'the library is full');
  const tooMany = await call('POST', '/v1/items', { key, body: { items: Array.from({ length: 51 }, (_, n) => reel(n, 'a')) } });
  assert.equal(tooMany.status, 400);
  let last;
  for (let i = 0; i < 4; i += 1) last = await call('GET', '/v1/usage', { key });
  assert.equal(last.status, 429);
});

test('a revoked key stops working; erasing removes the texts', async () => {
  const { env, call, customer } = stack();
  const a = await customer('Kunde A');
  await call('POST', '/v1/items', { key: a.key, body: { items: [reel(1, 'bleibt nicht')] } });
  assert.equal((await call('DELETE', `/v1/admin/customers/${a.customer}?erase=true`, { key: env.ADMIN_TOKEN })).status, 200);
  assert.equal((await call('GET', '/v1/items', { key: a.key })).status, 401);
  const listed = await call('GET', '/v1/admin/customers', { key: env.ADMIN_TOKEN });
  assert.ok(listed.data.customers[0].revoked_at);
  assert.equal(JSON.stringify(listed.data).includes(a.key), false, 'the directory never holds a key');
});

test('broken input is refused with a reason, not a crash', async () => {
  const { call, customer } = stack();
  const { key } = await customer('Kunde A');
  const r = await call('POST', '/v1/items', { key, body: { items: [{ url: 'javascript:alert(1)', text: 'x' }, { url: 'https://a.test/v', text: '' }, null] } });
  assert.equal(r.status, 200);
  assert.equal(r.data.rejected.length, 3);
  assert.equal(normalize({ url: 'https://a.test/v', text: 'ok', platform: 'Evil Platform' }, 1000).item.platform, 'other');
  const notJson = await worker.fetch(new Request('https://library.test/v1/items', {
    method: 'POST', headers: { authorization: `Bearer ${key}` }, body: '{',
  }), (await stack()).env);
  assert.equal(notJson.status, 401, 'an unknown customer is refused before its body is read');
});

test('the same text saved again writes nothing; a changed one is written', async () => {
  const { call, customer } = stack();
  const { key } = await customer('Kunde A');
  const first = await call('POST', '/v1/items', { key, body: { items: [reel(1, 'Erbschaftsteuer sparen')] } });
  assert.ok(first.data.rows_written > 0);
  const again = await call('POST', '/v1/items', { key, body: { items: [reel(1, 'Erbschaftsteuer sparen')] } });
  assert.equal(again.data.saved[0].unchanged, true);
  assert.equal(again.data.rows_written, 0);
  const changed = await call('POST', '/v1/items', { key, body: { items: [reel(1, 'Erbschaftsteuer sparen 2027')] } });
  assert.equal(changed.data.saved[0].unchanged, undefined);
  assert.ok(changed.data.rows_written > 0);
});

test('a kept text is found by its platform and video id', async () => {
  const { call, customer } = stack();
  const { key } = await customer('Kunde A');
  await call('POST', '/v1/items', { key, body: { items: [reel(7, 'Notarkosten')] } });
  const found = await call('GET', '/v1/item?platform=instagram&external_id=R7', { key });
  assert.equal(found.data.text, 'Notarkosten');
  assert.equal(found.data.sha, undefined, 'the content hash stays inside');
  assert.equal((await call('GET', '/v1/item?platform=tiktok&external_id=R7', { key })).status, 404);
});

test('a database made before the content hash gets it, and keeps its texts', async () => {
  const s = storage();
  s.db.exec('create table item (id integer primary key, url text not null unique, platform text, kind text,'
    + ' external_id text, title text, author text, published_at text, duration_seconds real, language text,'
    + ' text text not null, segments text, meta text, bytes integer not null, created_at text not null, updated_at text not null)');
  s.db.exec("insert into item (url, text, bytes, created_at, updated_at) values ('https://a.test/1', 'alt', 3, 'x', 'x')");
  s.db.exec('create table account (id integer primary key check (id = 1), hash text not null, name text, created_at text not null, revoked_at text)');
  new Library({ storage: s }, {});
  const columns = s.db.prepare("select name from pragma_table_info('item')").all().map((c) => c.name);
  assert.ok(columns.includes('sha'));
  assert.equal(s.db.prepare('select text from item').get().text, 'alt');
  assert.ok(s.db instanceof DatabaseSync);
});

test('the day\'s requests are written every 50 requests, not on every one', async () => {
  const { env, call, customer } = stack();
  const { key, customer: id } = await customer('Kunde A');
  const db = env.LIBRARY.get(id).ctx.storage.db;
  for (let i = 0; i < 49; i += 1) await call('GET', '/v1/items', { key });
  assert.equal(db.prepare('select count(*) as n from usage_day').get().n, 0);
  await call('GET', '/v1/items', { key });
  assert.equal(db.prepare('select requests from usage_day').get().requests, 50);
  assert.equal((await call('GET', '/v1/usage', { key })).data.requests_today, 51);
});

test('a key-shaped token with an invented customer id writes nothing anywhere', async () => {
  const { env, call } = stack();
  const invented = 'abcdefghij012345';
  const r = await call('GET', '/v1/items', { key: `sml_${invented}_${'a'.repeat(48)}` });
  assert.equal(r.status, 401);
  const db = env.LIBRARY.get(invented).ctx.storage.db;
  assert.equal(db.prepare('select count(*) as n from sqlite_master').get().n, 0, 'no table was made');
});

test('deleting frees room in a full library, and an erased customer stays empty', async () => {
  const { env, call, customer } = stack({ MAX_CUSTOMER_BYTES: '900' });
  const a = await customer('Kunde A');
  await call('POST', '/v1/items', { key: a.key, body: { items: [reel(1, 'y'.repeat(150)), reel(2, 'z'.repeat(150))] } });
  const full = await call('POST', '/v1/items', { key: a.key, body: { items: [reel(3, 'w'.repeat(150))] } });
  assert.equal(full.data.rejected[0].reason, 'the library is full');
  await call('DELETE', `/v1/item?url=${encodeURIComponent(reel(1, '').url)}`, { key: a.key });
  const room = await call('POST', '/v1/items', { key: a.key, body: { items: [reel(3, 'w'.repeat(150))] } });
  assert.equal(room.data.saved.length, 1);
  await call('DELETE', `/v1/admin/customers/${a.customer}?erase=true`, { key: env.ADMIN_TOKEN });
  const db = env.LIBRARY.get(a.customer).ctx.storage.db;
  assert.equal(db.prepare('select count(*) as n from sqlite_master').get().n, 0);
});

// The operator: the AWG hub keeps one library per hub user with one token.
const OPERATOR = 'o'.repeat(40);
async function asOperator(env, method, path, { customer, name, token = OPERATOR, body, key } = {}) {
  const r = await worker.fetch(new Request(`https://library.test${path}`, {
    method,
    headers: {
      authorization: `Bearer ${key ?? token}`,
      'content-type': 'application/json',
      ...(customer ? { 'x-library-customer': customer } : {}),
      ...(name ? { 'x-library-name': name } : {}),
    },
    ...(body !== undefined ? { body: JSON.stringify(body) } : {}),
  }), env);
  return { status: r.status, data: await r.json() };
}
const operatorEnv = async (vars = {}) => {
  const { createHash } = await import('node:crypto');
  return stack({ OPERATOR_TOKEN_SHA256: createHash('sha256').update(OPERATOR).digest('hex'), ...vars });
};

test('the operator keeps a library per member, made on first use and listed for the admin', async () => {
  const { env, call } = await operatorEnv();
  const ann = 'annannannannann1';
  const bob = 'bobbobbobbobbob2';
  assert.equal((await asOperator(env, 'GET', '/v1/items', { customer: ann, name: 'hub user Ann' })).data.items.length, 0);
  await asOperator(env, 'POST', '/v1/items', { customer: ann, body: { items: [reel(1, 'Die Grunderwerbsteuer in Bayern')] } });
  assert.equal((await asOperator(env, 'GET', '/v1/search?q=Grunderwerbsteuer', { customer: ann })).data.results.length, 1);
  assert.equal((await asOperator(env, 'GET', '/v1/search?q=Grunderwerbsteuer', { customer: bob })).data.results.length, 0,
    'another member sees nothing of it');
  const listed = (await call('GET', '/v1/admin/customers', { key: env.ADMIN_TOKEN })).data.customers.map((c) => [c.id, c.name]);
  assert.deepEqual(listed, [[ann, 'hub user Ann'], [bob, `member ${bob}`]]);
});

test('the operator reaches a customer the admin made, so a hub user can keep an existing library', async () => {
  const { env, call, customer } = await operatorEnv();
  const a = await customer('Kunde A');
  await call('POST', '/v1/items', { key: a.key, body: { items: [reel(1, 'Notarkosten beim Hauskauf')] } });
  assert.equal((await asOperator(env, 'GET', '/v1/search?q=Notarkosten', { customer: a.customer })).data.results.length, 1);
});

test('the operator path is closed without the right token, without a library name, and when no operator is set', async () => {
  const { env } = await operatorEnv();
  assert.equal((await asOperator(env, 'GET', '/v1/items', { customer: 'annannannannann1', token: 'x'.repeat(40) })).status, 401);
  assert.equal((await asOperator(env, 'GET', '/v1/items', {})).status, 400);
  assert.equal((await asOperator(env, 'GET', '/v1/items', { customer: '../../etc/passwd' })).status, 400);
  const none = stack();
  assert.equal((await asOperator(none.env, 'GET', '/v1/items', { customer: 'annannannannann1' })).status, 401);
});

test('a caller cannot claim the operator path with its own header', async () => {
  const { env, customer } = await operatorEnv();
  const a = await customer('Kunde A');
  const forged = await worker.fetch(new Request('https://library.test/v1/items', {
    headers: { authorization: `Bearer sml_annannannannann1_${'0'.repeat(48)}`, 'x-library-via': 'operator',
      'x-library-customer': 'annannannannann1' },
  }), env);
  assert.equal(forged.status, 401, 'a made-up key with the header stays refused');
  const db = env.LIBRARY.get('annannannannann1').ctx.storage.db;
  assert.equal(db.prepare('select count(*) as n from sqlite_master').get().n, 0, 'and nothing was made');
  assert.ok(a.key);
});

test('a revoked member stays refused; an erased one starts empty', async () => {
  const { env, call } = await operatorEnv();
  const ann = 'annannannannann1';
  await asOperator(env, 'POST', '/v1/items', { customer: ann, body: { items: [reel(1, 'Mietrecht')] } });
  await call('DELETE', `/v1/admin/customers/${ann}`, { key: env.ADMIN_TOKEN });
  assert.equal((await asOperator(env, 'GET', '/v1/items', { customer: ann })).status, 401);
  await call('DELETE', `/v1/admin/customers/${ann}?erase=true`, { key: env.ADMIN_TOKEN });
  assert.equal((await asOperator(env, 'GET', '/v1/items', { customer: ann })).data.items.length, 0);
});
