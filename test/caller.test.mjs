// A server for many people (the AWG hub): every call names its person in
// _meta["stardawn/caller"], and that person's library and budget share are used.
// Against the real library worker in-process (node:sqlite for Cloudflare's SQLite).
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import worker from '../library/worker.mjs';
import { cloudflareEnv } from '../library/fake-cloudflare.mjs';
import { callerOf, libraryIdOf, memberOf } from '../src/caller.mjs';
import { libraryFromEnv } from '../src/library.mjs';
import { tools } from '../src/mcp-server.mjs';
import { runImagesTool, runSocialTool } from '../src/social.mjs';

const OPERATOR = 'o'.repeat(40);
function hub(extra = {}) {
  const cf = cloudflareEnv({ OPERATOR_TOKEN_SHA256: createHash('sha256').update(OPERATOR).digest('hex') });
  const fetchImpl = (url, init) => worker.fetch(new Request(url, init), cf);
  const env = { SMTRANSCRIPT_LIBRARY_URL: 'https://library.test', SMTRANSCRIPT_LIBRARY_OPERATOR_TOKEN: OPERATOR, ...extra };
  const library = libraryFromEnv(env, fetchImpl);
  const tool = (name, caller) => tools(env, library, caller).find((t) => t.tool.name === name).run;
  return { cf, fetchImpl, library, tool };
}

const reel = (id, text) => ({
  job_id: id, kind: 'video', status: 'completed', platform_requests: 1, warnings: [],
  media: { platform: 'instagram', platform_id: id, canonical_url: `https://www.instagram.com/reel/${id}/`, author: 'immo.kanal', duration_seconds: 20 },
  artifacts: [],
  transcript: { status: 'available', source: 'asr', language: 'de', model: 'groq', text, segments: [{ start_ms: 0, end_ms: 5000, text }] },
});

test('the caller is read from _meta and nowhere else', () => {
  assert.deepEqual(callerOf({ _meta: { 'stardawn/caller': { id: 'u1', name: 'Ann' } } }), { id: 'u1', name: 'Ann' });
  assert.equal(callerOf({ arguments: { caller: 'u1' } }), null);
  assert.equal(callerOf({ _meta: { 'stardawn/caller': { id: '' } } }), null);
  assert.equal(callerOf({ _meta: { 'stardawn/caller': { id: 'x'.repeat(201) } } }), null);
  assert.equal(memberOf({ id: 'abcDEF123' }), 'user:abcDEF123');
  assert.match(memberOf({ id: 'ann bob/../x' }), /^user:[a-f0-9]{32}$/);
  assert.equal(libraryIdOf({ id: 'u1' }, { u1: 'jimjimjimjimjim1' }), 'jimjimjimjimjim1');
  assert.match(libraryIdOf({ id: 'u2' }, { u1: 'jimjimjimjimjim1' }), /^[a-f0-9]{16}$/);
});

test('two hub users keep and find only their own texts', async () => {
  const { tool } = hub();
  const ann = { id: 'user-ann' };
  const bob = { id: 'user-bob' };
  const api = { submit: async () => ({ job_id: 'R1', existing: false }), result: async () => ({ status: 200, data: reel('R1', 'Grunderwerbsteuer in Bayern') }) };
  const deps = { worker: { url: 'x', key: 'k' }, api, pollMs: 0, output: 'inline' };
  await tool('get_social_transcript', ann)({ url: 'https://www.instagram.com/reel/R1/' }, { caller: ann }, deps);
  assert.equal((await tool('library_search', ann)({ query: 'Grunderwerbsteuer' })).results.length, 1);
  assert.equal((await tool('library_search', bob)({ query: 'Grunderwerbsteuer' })).results.length, 0, 'Bob sees nothing of Ann');
  assert.equal((await tool('library_list', bob)({})).items.length, 0);
});

test('a hub user can be given the library that exists already', async () => {
  const { cf, fetchImpl } = hub();
  const made = await (await fetchImpl('https://library.test/v1/admin/customers', {
    method: 'POST', headers: { authorization: `Bearer ${cf.ADMIN_TOKEN}` }, body: JSON.stringify({ name: 'Jim' }),
  })).json();
  await fetchImpl('https://library.test/v1/items', { method: 'POST', headers: { authorization: `Bearer ${made.key}` },
    body: JSON.stringify({ items: [{ url: 'https://www.instagram.com/reel/J1/', platform: 'instagram', text: 'Notarkosten beim Hauskauf' }] }) });
  // the same Cloudflare stand-in, so the mapped library is the one made above
  const env = { SMTRANSCRIPT_LIBRARY_URL: 'https://library.test', SMTRANSCRIPT_LIBRARY_OPERATOR_TOKEN: OPERATOR,
    SMTRANSCRIPT_LIBRARY_MEMBERS: JSON.stringify({ 'user-jim': made.customer }) };
  const library = libraryFromEnv(env, fetchImpl);
  const search = tools(env, library, { id: 'user-jim' }).find((t) => t.tool.name === 'library_search').run;
  assert.equal((await search({ query: 'Notarkosten' })).results.length, 1);
});

test('a call that names nobody has no library and keeps nothing', async () => {
  const { tool } = hub();
  await assert.rejects(() => tool('library_search', null)({ query: 'x' }), /kept per AWG user/);
  const api = { submit: async () => ({ job_id: 'R2', existing: false }), result: async () => ({ status: 200, data: reel('R2', 'Mietrecht') }) };
  const out = await tool('get_social_transcript', null)({ url: 'https://www.instagram.com/reel/R2/' }, {},
    { worker: { url: 'x', key: 'k' }, api, pollMs: 0, output: 'inline' });
  assert.equal(out.library, undefined, 'nothing was kept for nobody');
  assert.throws(() => libraryFromEnv({ SMTRANSCRIPT_LIBRARY_URL: 'https://l.test', SMTRANSCRIPT_LIBRARY_OPERATOR_TOKEN: OPERATOR,
    SMTRANSCRIPT_LIBRARY_MEMBERS: '{oops' }), /not JSON/);
});

test('the worker job is charged to the caller who asked', async () => {
  const sent = [];
  const api = { submit: async (body) => { sent.push(body); return { job_id: 'R3', existing: false }; },
    result: async () => ({ status: 200, data: { ...reel('R3', 'x'), kind: 'images', images: [] } }) };
  const deps = { worker: { url: 'x', key: 'k' }, api, pollMs: 0, output: 'inline' };
  await runSocialTool({ url: 'https://www.instagram.com/reel/R3/' }, { caller: { id: 'user-ann' } }, deps);
  await runImagesTool({ url: 'https://www.instagram.com/p/P3/' }, { caller: { id: 'user-ann' } }, deps).catch(() => {});
  await runSocialTool({ url: 'https://www.instagram.com/reel/R3/' }, {}, deps);
  assert.deepEqual(sent.map((b) => b.member), ['user:user-ann', 'user:user-ann', undefined]);
});

test('an Instagram video comes with the platform\'s own mp4 link', async () => {
  const { shape } = await import('../src/social.mjs');
  const r = reel('R9', 'x');
  r.media.video_url = 'https://scontent.cdninstagram.com/v/t50/abc.mp4?oe=1';
  r.media.video_url_read_at = '2026-11-14T15:00:00Z';
  const out = shape(r);
  assert.equal(out.videoDownload.url, r.media.video_url);
  assert.match(out.videoDownload.note, /stops working/);
  assert.equal(shape(reel('R8', 'x')).videoDownload, undefined);
});

test('a result that is ready soon is picked up within about a second', async () => {
  const { waitResult } = await import('../src/social.mjs');
  let asked = 0;
  const api = { result: async () => (++asked < 3 ? { status: 202, data: { status: 'fetching', retry_after_s: 3 } } : { status: 200, data: { ok: true } }) };
  const started = Date.now();
  assert.deepEqual(await waitResult(api, 'job', { seconds: 60 }), { ok: true });
  assert.ok(Date.now() - started < 3000, `took ${Date.now() - started} ms; asked every 3 s it would be 6 s`);
});

test('asking for the mp4 link goes past the library to the worker, once a day', async () => {
  const { tool } = hub();
  const ann = { id: 'user-ann' };
  const sent = [];
  const api = { submit: async (body) => { sent.push(body); return { job_id: 'R7', existing: false }; },
    result: async () => ({ status: 200, data: reel('R7', 'Kaufnebenkosten') }) };
  const deps = { worker: { url: 'x', key: 'k' }, api, pollMs: 0, output: 'inline' };
  const url = 'https://www.instagram.com/reel/R7/';
  await tool('get_social_transcript', ann)({ url }, { caller: ann }, deps);
  await tool('get_social_transcript', ann)({ url }, { caller: ann }, deps);
  assert.equal(sent.length, 1, 'the second ask is answered from the library');
  await tool('get_social_transcript', ann)({ url, video_link: true }, { caller: ann }, deps);
  assert.equal(sent.length, 2, 'asking for the link goes to the worker');
  assert.equal(sent[1].video_link, true);
  assert.match(sent[1].idempotency_key, /^video-link:https:\/\/www\.instagram\.com\/reel\/R7\/:\d{4}-\d{2}-\d{2}$/);
});
