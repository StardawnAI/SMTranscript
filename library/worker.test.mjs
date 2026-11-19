// node:sqlite stands in for a Durable Object's SQLite, so schema, FTS5 and
// triggers run for real; only the Cloudflare plumbing around them is faked.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { DatabaseSync } from 'node:sqlite';
import worker, { Library, MAX_DOWNLOAD_BYTES, PART_SIZE, byteRange, downloadName, ftsQuery, normalize } from './worker.mjs';
import { cloudflareEnv, fireAlarms, storage } from './fake-cloudflare.mjs';

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

test('texts go into folders; list and search per folder; the folders are listed with their counts', async () => {
  const { call, customer } = stack();
  const { key } = await customer('Kunde A');
  await call('POST', '/v1/items', { key, body: { items: [{ ...reel(1, 'Grunderwerbsteuer'), folder: 'Immobilien' },
    { ...reel(2, 'Grunderwerbsteuer Tipps'), folder: ' immobilien  ' }, reel(3, 'Grunderwerbsteuer am Rande')] } });
  const folders = (await call('GET', '/v1/folders', { key })).data.folders;
  assert.deepEqual(folders.map((f) => [f.name, f.items]), [['General', 1], ['Immobilien', 2]], 'case and spaces do not split a folder');
  assert.equal((await call('GET', '/v1/items?folder=immobilien', { key })).data.items.length, 2);
  assert.equal((await call('GET', '/v1/search?q=Grunderwerbsteuer&folder=General', { key })).data.results.length, 1);
  assert.equal((await call('GET', '/v1/search?q=Grunderwerbsteuer', { key })).data.results.length, 3);
});

test('asking for a kept text into another folder moves it without rewriting it; a save without a folder keeps it', async () => {
  const { call, customer } = stack();
  const { key } = await customer('Kunde A');
  await call('POST', '/v1/items', { key, body: { items: [reel(1, 'Mietrecht')] } });
  const moved = await call('POST', '/v1/items', { key, body: { items: [{ ...reel(1, 'Mietrecht'), folder: 'Recht' }] } });
  assert.equal(moved.data.saved[0].moved, true);
  assert.ok(moved.data.rows_written <= 2, `a move writes the row, not the search index again (${moved.data.rows_written})`);
  await call('POST', '/v1/items', { key, body: { items: [reel(1, 'Mietrecht neu')] } });
  assert.equal((await call('GET', '/v1/items?folder=Recht', { key })).data.items.length, 1, 'a new text without a folder stays where it was');
});

test('one text is moved, a whole folder is renamed', async () => {
  const { call, customer } = stack();
  const { key } = await customer('Kunde A');
  await call('POST', '/v1/items', { key, body: { items: [reel(1, 'a'), reel(2, 'b')] } });
  const one = await call('POST', '/v1/move', { key, body: { url: reel(1, '').url, folder: 'Immobilien' } });
  assert.equal(one.data.folder, 'Immobilien');
  const renamed = await call('POST', '/v1/move', { key, body: { from: 'general', to: 'Später' } });
  assert.equal(renamed.data.to, 'Später');
  const folders = (await call('GET', '/v1/folders', { key })).data.folders.map((f) => f.name);
  assert.deepEqual(folders, ['Immobilien', 'Später']);
  assert.equal((await call('POST', '/v1/move', { key, body: { url: 'https://nope.example/x', folder: 'X' } })).status, 404);
  assert.equal((await call('POST', '/v1/move', { key, body: { id: 1 } })).status, 400);
});

const SCHEMA_DEF = {
  idea: 'string', legal_basis: [{ law: 'string', section: 'string' }], who: ['alleine', 'ehepartner_noetig', 'gmbh'],
};

test('a schema hands out the folder\'s texts without a result, oldest first, until every one has one', async () => {
  const { call, customer } = stack();
  const { key } = await customer('Kunde A');
  const immo = [1, 2, 3].map((n) => ({ ...reel(n, `Tipp ${n}: Spekulationsfrist nach zehn Jahren`), folder: 'Immobilien' }));
  await call('POST', '/v1/items', { key, body: { items: [...immo, reel(9, 'Anderes Thema')] } });
  const made = await call('POST', '/v1/schemas', { key, body: { name: 'Invest-Ideen', folder: 'immobilien', definition: SCHEMA_DEF } });
  assert.equal(made.status, 200);
  assert.equal(made.data.schema.folder, 'Immobilien');
  assert.deepEqual([made.data.schema.texts, made.data.schema.extracted, made.data.schema.remaining], [3, 0, 3]);

  const first = (await call('GET', '/v1/schema/next?name=invest-ideen&limit=2', { key })).data;
  assert.deepEqual(first.schema.definition, SCHEMA_DEF);
  assert.deepEqual(first.items.map((i) => i.text.slice(0, 6)), ['Tipp 1', 'Tipp 2']);
  const saved = await call('POST', '/v1/extractions', { key, body: { schema: 'Invest-Ideen', results: first.items.map((i) => ({
    item_id: i.id, result: { idea: `Idee ${i.id}`, legal_basis: [{ law: 'EStG', section: '§ 23' }], who: 'alleine' },
  })) } });
  assert.deepEqual([saved.data.saved.length, saved.data.extracted, saved.data.remaining], [2, 2, 1]);

  const next = (await call('GET', '/v1/schema/next?name=Invest-Ideen', { key })).data;
  assert.deepEqual(next.items.map((i) => i.text.slice(0, 6)), ['Tipp 3']);
  const results = (await call('GET', '/v1/extractions?name=Invest-Ideen', { key })).data;
  assert.equal(results.results.length, 2);
  assert.equal(results.results[0].result.legal_basis[0].section, '§ 23');
  assert.equal(results.results[0].url, immo[1].url);
});

test('saving a result twice keeps one; results go when their text or their schema goes', async () => {
  const { call, customer } = stack();
  const { key } = await customer('Kunde A');
  await call('POST', '/v1/items', { key, body: { items: [{ ...reel(1, 'Tipp'), folder: 'Immo' }, { ...reel(2, 'Tipp zwei'), folder: 'Immo' }] } });
  await call('POST', '/v1/schemas', { key, body: { name: 'S', folder: 'Immo', definition: 'Pull out the idea.' } });
  const save = (id, result) => call('POST', '/v1/extractions', { key, body: { schema: 'S', results: [{ item_id: id, result }] } });
  assert.equal((await save(1, 'erste Fassung')).data.saved[0].created, true);
  assert.equal((await save(1, 'zweite Fassung')).data.saved[0].created, false);
  await save(2, { idea: 'b' });
  let list = (await call('GET', '/v1/extractions?name=S', { key })).data.results;
  assert.deepEqual(list.map((r) => r.result), [{ idea: 'b' }, 'zweite Fassung']);

  await call('DELETE', '/v1/item?id=2', { key });
  list = (await call('GET', '/v1/extractions?name=S', { key })).data.results;
  assert.deepEqual(list.map((r) => r.item_id), [1]);
  const gone = await call('DELETE', '/v1/schema?name=S', { key });
  assert.deepEqual(gone.data, { deleted: 'S', results_deleted: 1 });
  assert.equal((await call('GET', '/v1/extractions?name=S', { key })).status, 404);
  assert.equal((await call('GET', '/v1/schemas', { key })).data.schemas.length, 0);
});

test('schemas and results are refused with a reason when broken, too big or foreign', async () => {
  const { call, customer } = stack();
  const a = await customer('Kunde A');
  const b = await customer('Kunde B');
  await call('POST', '/v1/items', { key: a.key, body: { items: [{ ...reel(1, 'Tipp'), folder: 'Immo' }] } });
  assert.equal((await call('POST', '/v1/schemas', { key: a.key, body: { folder: 'Immo', definition: 'x' } })).status, 400);
  assert.equal((await call('POST', '/v1/schemas', { key: a.key, body: { name: 'S', definition: 'x' } })).status, 400);
  assert.equal((await call('POST', '/v1/schemas', { key: a.key, body: { name: 'S', folder: 'Immo' } })).status, 400);
  assert.equal((await call('POST', '/v1/schemas', { key: a.key, body: { name: 'S', folder: 'Immo', definition: 'x'.repeat(20001) } })).status, 400);
  await call('POST', '/v1/schemas', { key: a.key, body: { name: 'S', folder: 'Immo', definition: 'x' } });
  const r = await call('POST', '/v1/extractions', { key: a.key, body: { schema: 'S', results: [
    { item_id: 1, result: 'x'.repeat(20001) }, { item_id: 777, result: 'y' }, { result: 'z' }, { item_id: 1 },
  ] } });
  assert.equal(r.data.saved.length, 0);
  assert.deepEqual(r.data.rejected.map((x) => x.reason), [
    'result over 20000 bytes', 'no such text in this library', 'item_id is required', 'result is required',
  ]);
  assert.equal((await call('POST', '/v1/extractions', { key: a.key, body: { schema: 'S', results: [] } })).status, 400);
  // Another customer neither sees the schema nor writes to it.
  assert.equal((await call('GET', '/v1/schema?name=S', { key: b.key })).status, 404);
  assert.equal((await call('POST', '/v1/extractions', { key: b.key, body: { schema: 'S', results: [{ item_id: 1, result: 'x' }] } })).status, 404);
});

// Downloads: the upload token puts a file into R2 in parts, the link serves it
// for one hour, the Downloads object's alarm deletes it after that.
function downloadStack(vars = {}) {
  const env = cloudflareEnv(vars);
  const send = (method, path, { token = env.UPLOAD_TOKEN, body, bytes, headers = {} } = {}) => worker.fetch(new Request(`https://library.test${path}`, {
    method,
    headers: {
      ...(token ? { authorization: `Bearer ${token}` } : {}),
      ...(bytes ? { 'content-length': String(bytes.length) } : body !== undefined ? { 'content-type': 'application/json' } : {}),
      ...headers,
    },
    ...(bytes ? { body: bytes } : body !== undefined ? { body: JSON.stringify(body) } : {}),
  }), env);
  const call = async (method, path, options) => {
    const r = await send(method, path, options);
    return { status: r.status, data: await r.json() };
  };
  const create = (body) => call('POST', '/v1/downloads', { body: { filename: 'clip.mp4', mime: 'video/mp4', title: 'A clip',
    source_url: 'https://www.youtube.com/watch?v=abc', ...body } });
  const rows = () => env.DOWNLOAD_INDEX.get('downloads').rows('select * from download order by created_at');
  return { env, send, call, create, rows, alarms: () => fireAlarms(env.DOWNLOAD_INDEX) };
}

const START = Date.parse('2026-11-19T10:00:00Z');
const filled = (length, seed) => {
  const bytes = new Uint8Array(length);
  for (let i = 0; i < length; i += 1) bytes[i] = (i * 7 + seed) % 251;
  return bytes;
};

test('a file goes up in parts, its link serves it with ranges for one hour, then the alarm deletes it', async (t) => {
  t.mock.timers.enable({ apis: ['Date'], now: START });
  const { env, send, call, create, alarms } = downloadStack();
  const size = PART_SIZE + 1000;
  const made = await create({ filename: 'Mein Video – Teil 1.mp4', size });
  assert.equal(made.status, 200);
  assert.match(made.data.id, /^[a-f0-9]{32}$/);
  assert.equal(made.data.part_size, PART_SIZE);
  assert.equal(made.data.parts, 2);
  const { id } = made.data;
  const first = filled(PART_SIZE, 1);
  const last = filled(1000, 2);
  assert.equal((await call('PUT', `/v1/downloads/${id}/parts/1`, { bytes: first })).status, 200);
  assert.equal((await call('PUT', `/v1/downloads/${id}/parts/2`, { bytes: last })).status, 200);
  const done = await call('POST', `/v1/downloads/${id}/complete`);
  assert.equal(done.status, 200);
  assert.equal(done.data.url, `https://library.test/d/${id}/Mein_Video_Teil_1.mp4`);
  assert.equal(done.data.expires_at, new Date(START + 3600 * 1000).toISOString());
  assert.equal(done.data.size, size);
  assert.equal(done.data.title, 'A clip');
  // A retried complete hands out the same link.
  assert.equal((await call('POST', `/v1/downloads/${id}/complete`)).data.url, done.data.url);
  const path = new URL(done.data.url).pathname;

  // The link needs no token.
  const whole = await send('GET', path, { token: null });
  assert.equal(whole.status, 200);
  assert.equal(whole.headers.get('content-type'), 'video/mp4');
  assert.equal(whole.headers.get('content-disposition'), 'attachment; filename="Mein_Video_Teil_1.mp4"');
  assert.equal(whole.headers.get('accept-ranges'), 'bytes');
  assert.equal(whole.headers.get('content-length'), String(size));
  const body = new Uint8Array(await whole.arrayBuffer());
  assert.equal(body.length, size);
  assert.deepEqual(body.subarray(PART_SIZE - 3, PART_SIZE + 3), new Uint8Array([...first.subarray(-3), ...last.subarray(0, 3)]));

  const range = await send('GET', path, { token: null, headers: { range: `bytes=${PART_SIZE - 2}-${PART_SIZE + 1}` } });
  assert.equal(range.status, 206);
  assert.equal(range.headers.get('content-range'), `bytes ${PART_SIZE - 2}-${PART_SIZE + 1}/${size}`);
  assert.deepEqual(new Uint8Array(await range.arrayBuffer()), new Uint8Array([...first.subarray(-2), ...last.subarray(0, 2)]));
  const tail = await send('GET', path, { token: null, headers: { range: 'bytes=-5' } });
  assert.equal(tail.status, 206);
  assert.deepEqual(new Uint8Array(await tail.arrayBuffer()), last.subarray(-5));
  const outside = await send('GET', path, { token: null, headers: { range: `bytes=${size}-` } });
  assert.equal(outside.status, 416);
  assert.equal(outside.headers.get('content-range'), `bytes */${size}`);
  const head = await send('HEAD', path, { token: null });
  assert.equal(head.status, 200);
  assert.equal(head.headers.get('content-length'), String(size));

  // A second before the hour is over it still works; the alarm waits for its time.
  t.mock.timers.tick(3599 * 1000);
  await alarms();
  assert.equal((await send('GET', path, { token: null, headers: { range: 'bytes=0-0' } })).status, 206);
  t.mock.timers.tick(1000);
  assert.equal((await send('GET', path, { token: null })).status, 410, 'expired before the alarm runs');
  await alarms();
  assert.equal(env.DOWNLOADS.objects.size, 0, 'the file left R2');
  assert.equal((await send('GET', path, { token: null })).status, 410);
  assert.equal((await send('HEAD', path, { token: null })).status, 410);
  // Nothing extends it: completing again does not bring it back.
  assert.equal((await call('POST', `/v1/downloads/${id}/complete`)).status, 409);
  assert.equal(await env.DOWNLOAD_INDEX.get('downloads').ctx.storage.getAlarm(), null, 'nothing left to wait for');
});

test('the upload routes need the upload token and refuse to run without one', async () => {
  const { call, create, send } = downloadStack();
  assert.equal((await call('POST', '/v1/downloads', { token: null, body: { filename: 'a.mp4', size: 10 } })).status, 401);
  assert.equal((await call('POST', '/v1/downloads', { token: 'w'.repeat(40), body: { filename: 'a.mp4', size: 10 } })).status, 401);
  assert.equal((await call('POST', '/v1/downloads', { token: 'sml_x', body: { filename: 'a.mp4', size: 10 } })).status, 401);
  const { id } = (await create({ size: 10 })).data;
  assert.equal((await call('PUT', `/v1/downloads/${id}/parts/1`, { token: 'w'.repeat(40), bytes: filled(10, 0) })).status, 401);
  assert.equal((await call('POST', `/v1/downloads/${id}/complete`, { token: null })).status, 401);
  assert.equal((await call('POST', `/v1/downloads/${id}/abort`, { token: null })).status, 401);
  assert.equal((await send('GET', '/v1/downloads', {})).status, 404);
  const unset = downloadStack({ UPLOAD_TOKEN: '' });
  assert.equal((await unset.call('POST', '/v1/downloads', { token: 'u'.repeat(40), body: { filename: 'a.mp4', size: 10 } })).status, 503);
});

test('the ceilings hold: 4 GiB a file, 100 GiB stored at once', async () => {
  const { create, call } = downloadStack();
  assert.equal((await create({ size: MAX_DOWNLOAD_BYTES + 1 })).status, 413);
  for (const size of [0, -1, 1.5, 'big', null]) assert.equal((await create({ size })).status, 400, `size ${size}`);
  const ids = [];
  for (let i = 0; i < 25; i += 1) {
    const r = await create({ size: MAX_DOWNLOAD_BYTES });
    assert.equal(r.status, 200, `file ${i + 1}`);
    ids.push(r.data.id);
  }
  const full = await create({ size: 1 });
  assert.equal(full.status, 507);
  assert.match(full.data.error, /full/);
  // An aborted upload frees its room.
  assert.equal((await call('POST', `/v1/downloads/${ids[0]}/abort`)).status, 200);
  assert.equal((await create({ size: 1 })).status, 200);
});

test('parts must be the right size and all there before complete', async () => {
  const { call, create } = downloadStack();
  const { id } = (await create({ size: PART_SIZE + 10 })).data;
  assert.equal((await call('PUT', `/v1/downloads/${id}/parts/2`, { bytes: filled(9, 0) })).status, 400);
  assert.equal((await call('PUT', `/v1/downloads/${id}/parts/3`, { bytes: filled(10, 0) })).status, 400);
  assert.equal((await call('PUT', `/v1/downloads/${id}/parts/0`, { bytes: filled(10, 0) })).status, 400);
  assert.equal((await call('PUT', `/v1/downloads/${id}/parts/2`, { bytes: filled(10, 0) })).status, 200);
  const early = await call('POST', `/v1/downloads/${id}/complete`);
  assert.equal(early.status, 409);
  assert.match(early.data.error, /parts missing: 1/);
  assert.equal((await call('PUT', `/v1/downloads/${'0'.repeat(32)}/parts/1`, { bytes: filled(10, 0) })).status, 404);
  assert.equal((await call('POST', `/v1/downloads/${'0'.repeat(32)}/complete`)).status, 404);
});

test('an aborted upload leaves nothing in R2 and takes no more parts', async () => {
  const { env, call, create, send, rows } = downloadStack();
  const { id } = (await create({ size: 20 })).data;
  assert.equal((await call('PUT', `/v1/downloads/${id}/parts/1`, { bytes: filled(20, 0) })).status, 200);
  assert.equal((await call('POST', `/v1/downloads/${id}/abort`)).status, 200);
  assert.equal((await call('POST', `/v1/downloads/${id}/abort`)).status, 200, 'aborting twice is fine');
  assert.equal([...env.DOWNLOADS.uploads.values()][0].state, 'aborted');
  assert.equal(env.DOWNLOADS.objects.size, 0);
  assert.equal(rows()[0].state, 'aborted');
  assert.equal((await call('PUT', `/v1/downloads/${id}/parts/1`, { bytes: filled(20, 0) })).status, 409);
  assert.equal((await call('POST', `/v1/downloads/${id}/complete`)).status, 409);
  assert.equal((await send('GET', `/d/${id}/clip.mp4`, { token: null })).status, 404);
});

test('an upload not completed within the hour is aborted by the alarm', async (t) => {
  t.mock.timers.enable({ apis: ['Date'], now: START });
  const { env, call, create, rows, alarms } = downloadStack();
  const { id } = (await create({ size: 20 })).data;
  assert.equal(await env.DOWNLOAD_INDEX.get('downloads').ctx.storage.getAlarm(), START + 3600 * 1000);
  assert.equal((await call('PUT', `/v1/downloads/${id}/parts/1`, { bytes: filled(20, 0) })).status, 200);
  t.mock.timers.tick(3600 * 1000);
  assert.equal((await call('POST', `/v1/downloads/${id}/complete`)).status, 410, 'too late even before the alarm');
  await alarms();
  assert.equal(rows()[0].state, 'aborted');
  assert.equal([...env.DOWNLOADS.uploads.values()][0].state, 'aborted');
  assert.equal(env.DOWNLOADS.objects.size, 0);
  assert.equal((await call('PUT', `/v1/downloads/${id}/parts/1`, { bytes: filled(20, 0) })).status, 409);
});

test('a link with a wrong id or file name finds nothing', async () => {
  const { call, create, send } = downloadStack();
  const { id } = (await create({ size: 5, filename: 'clip.mp4' })).data;
  await call('PUT', `/v1/downloads/${id}/parts/1`, { bytes: filled(5, 0) });
  await call('POST', `/v1/downloads/${id}/complete`);
  assert.equal((await send('GET', `/d/${id}/clip.mp4`, { token: null })).status, 200);
  assert.equal((await send('GET', `/d/${id}/other.mp4`, { token: null })).status, 404);
  assert.equal((await send('GET', `/d/${'0'.repeat(32)}/clip.mp4`, { token: null })).status, 404);
  assert.equal((await send('GET', `/d/${id}/%E0%A4%A`, { token: null })).status, 404);
  assert.equal((await send('GET', '/d/short/clip.mp4', { token: null })).status, 404);
  assert.equal((await send('DELETE', `/d/${id}/clip.mp4`, { token: null })).status, 405);
});

test('file names become safe ASCII with their extension, up to 150 characters', () => {
  assert.equal(downloadName('Mein Video – Teil 1.mp4'), 'Mein_Video_Teil_1.mp4');
  assert.equal(downloadName('Café "über" <alles>.MP4'), 'Cafe_uber_alles.MP4');
  assert.equal(downloadName('../../etc/passwd'), 'passwd');
  assert.equal(downloadName('C:\\Videos\\clip.mp4'), 'clip.mp4');
  assert.equal(downloadName('日本語.webm'), 'download.webm');
  assert.equal(downloadName(''), 'download');
  assert.equal(downloadName(undefined), 'download');
  assert.equal(downloadName('.hidden'), 'hidden');
  assert.equal(downloadName('noext'), 'noext');
  const long = downloadName(`${'a'.repeat(300)}.mp4`);
  assert.equal(long.length, 150);
  assert.ok(long.endsWith('.mp4'));
  assert.match(downloadName('x\r\n";filename=evil.exe .mp4'), /^[A-Za-z0-9._-]+$/);
});

test('byte ranges are read as R2 takes them', () => {
  assert.deepEqual(byteRange('bytes=0-9', 100), { offset: 0, length: 10 });
  assert.deepEqual(byteRange('bytes=90-', 100), { offset: 90, length: 10 });
  assert.deepEqual(byteRange('bytes=95-200', 100), { offset: 95, length: 5 });
  assert.deepEqual(byteRange('bytes=-10', 100), { offset: 90, length: 10 });
  assert.deepEqual(byteRange('bytes=-500', 100), { offset: 0, length: 100 });
  assert.equal(byteRange(null, 100), null);
  assert.equal(byteRange('bytes=0-1,5-6', 100), null);
  assert.equal(byteRange('items=0-1', 100), null);
  assert.equal(byteRange('bytes=100-', 100), 'unsatisfiable');
  assert.equal(byteRange('bytes=-0', 100), 'unsatisfiable');
  assert.equal(byteRange('bytes=5-2', 100), 'unsatisfiable');
});
