// node:sqlite stands in for a Durable Object's SQLite, so schema, FTS5 and
// triggers run for real; only the Cloudflare plumbing around them is faked.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import worker, { ftsQuery, normalize } from './worker.mjs';
import { cloudflareEnv } from './fake-cloudflare.mjs';

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
