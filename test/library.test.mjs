// The MCP side of the library, against the real library worker (in-process,
// node:sqlite in place of Cloudflare's SQLite): what the tools fetch is kept,
// found, read and deleted per customer.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import worker from '../library/worker.mjs';
import { cloudflareEnv } from '../library/fake-cloudflare.mjs';
import { keeping, libraryClient, libraryFromEnv, postRecord } from '../src/library.mjs';
import { collectTranscripts, tools } from '../src/mcp-server.mjs';
import { runImagesTool, runSocialTool } from '../src/social.mjs';

async function libraryFor(name, env = cloudflareEnv()) {
  const fetchImpl = (url, init) => worker.fetch(new Request(url, init), env);
  const made = await fetchImpl('https://library.test/v1/admin/customers', {
    method: 'POST', headers: { authorization: `Bearer ${env.ADMIN_TOKEN}` }, body: JSON.stringify({ name }),
  });
  const { key } = await made.json();
  return { env, library: libraryClient({ url: 'https://library.test', key }, fetchImpl) };
}

const reelResult = (id, text) => ({
  job_id: id, kind: 'video', status: 'completed', platform_requests: 1, warnings: [],
  media: { platform: 'instagram', platform_id: id, canonical_url: `https://www.instagram.com/reel/${id}/`, author: 'immo.kanal', duration_seconds: 20 },
  artifacts: [],
  transcript: { status: 'available', source: 'asr', language: 'de', model: 'gpt-4o-mini-transcribe', text, segments: [{ start_ms: 0, end_ms: 5000, text }] },
});

function fakeApi(results) {
  return {
    submit: async (body) => ({ job_id: Object.keys(results)[0], existing: false, body }),
    result: async (id) => ({ status: 200, data: results[id] }),
  };
}
const deps = (api) => ({ worker: { url: 'x', key: 'k' }, api, pollMs: 0, output: 'inline' });

test('without library settings the server offers what it offered before', () => {
  assert.equal(libraryFromEnv({}), null);
  assert.equal(libraryFromEnv({ SMTRANSCRIPT_LIBRARY_URL: 'https://l.test' }), null, 'a link without a key is no library');
  const names = tools({}, null).map((t) => t.tool.name);
  assert.equal(names.some((n) => n.startsWith('library_')), false);
});

test('with a library the four library tools come along', () => {
  const library = libraryFromEnv({ SMTRANSCRIPT_LIBRARY_URL: 'https://l.test/', SMTRANSCRIPT_LIBRARY_KEY: 'sml_x' });
  const names = tools({}, library).map((t) => t.tool.name);
  for (const n of ['library_search', 'library_list', 'library_get', 'library_delete']) assert.ok(names.includes(n), n);
});

test('a fetched reel is kept, found by a word, read in full and deleted', async () => {
  const { library } = await libraryFor('Kunde A');
  const run = keeping(runSocialTool, library);
  const out = await run({ url: 'https://www.instagram.com/reel/ABC123/' }, {}, deps(fakeApi({ ABC123: reelResult('ABC123', 'Die Spekulationsfrist bei Immobilien beträgt zehn Jahre') })));
  assert.deepEqual(out.library, { kept: 1 });

  const byName = Object.fromEntries(tools({}, library).map((t) => [t.tool.name, t.run]));
  const found = await byName.library_search({ query: 'spekulation immobilie' });
  assert.equal(found.results.length, 1);
  assert.equal(found.results[0].author, 'immo.kanal');
  const full = await byName.library_get({ id: found.results[0].id });
  assert.equal(full.text, 'Die Spekulationsfrist bei Immobilien beträgt zehn Jahre');
  assert.deepEqual(full.segments[0], { start_ms: 0, end_ms: 5000, text: full.text });
  assert.equal(full.meta.model, 'gpt-4o-mini-transcribe');
  const listed = await byName.library_list({});
  assert.equal(listed.items.length, 1);
  assert.equal(listed.usage.items, 1);
  await byName.library_delete({ url: 'https://www.instagram.com/reel/ABC123/' });
  assert.equal((await byName.library_search({ query: 'Spekulationsfrist' })).results.length, 0);
});

test('fetching the same reel again does not keep it twice', async () => {
  const { library } = await libraryFor('Kunde A');
  const run = keeping(runSocialTool, library);
  const api = fakeApi({ R1: reelResult('R1', 'Mietrendite berechnen') });
  await run({ url: 'https://www.instagram.com/reel/R1/' }, {}, deps(api));
  await run({ url: 'https://www.instagram.com/reel/R1/' }, {}, deps(api));
  assert.equal((await library.usage()).items, 1);
});

test('two customers each see only their own library', async () => {
  const env = cloudflareEnv();
  const a = await libraryFor('Kunde A', env);
  const b = await libraryFor('Kunde B', env);
  await keeping(runSocialTool, a.library)({ url: 'https://www.instagram.com/reel/R1/' }, {}, deps(fakeApi({ R1: reelResult('R1', 'Grundsteuerreform') })));
  assert.equal((await b.library.search({ q: 'Grundsteuerreform' })).results.length, 0);
  assert.equal((await a.library.search({ q: 'Grundsteuerreform' })).results.length, 1);
});

test('a picture post keeps its caption and the text on its pictures, not the pictures', async () => {
  const { library } = await libraryFor('Kunde A');
  const result = {
    job_id: 'P1', kind: 'images', status: 'completed', platform_requests: 1,
    media: { platform: 'instagram', platform_id: 'P1', canonical_url: 'https://www.instagram.com/p/P1/', post_kind: 'carousel', author: 'immo.dommi',
      description: 'Drei Tipps zur Finanzierung', image_urls: ['https://scontent.cdninstagram.com/1.jpg', 'https://scontent.cdninstagram.com/2.jpg'] },
    transcript: { status: 'available', source: 'ocr', text: 'Eigenkapital', segments: [{ image: 1, text: 'Eigenkapital' }, { image: 2, text: '' }] },
  };
  const out = await keeping(runImagesTool, library)({ url: 'https://www.instagram.com/p/P1/', include_images: false }, {}, deps(fakeApi({ P1: result })));
  assert.equal(out.library.kept, 1);
  const kept = await library.get({ url: 'https://www.instagram.com/p/P1/' });
  assert.equal(kept.text, 'Caption:\nDrei Tipps zur Finanzierung\n\nPicture 1:\nEigenkapital');
  assert.equal(kept.kind, 'post');
  assert.equal(JSON.stringify(kept).includes('cdninstagram'), false, 'no picture links are kept');
  assert.equal(postRecord({ url: 'u', images: [] }).text, '', 'a post without any text has nothing to keep');
});

test('a library that is down never costs the transcript, and says so once', async () => {
  const broken = libraryClient({ url: 'https://library.test', key: 'sml_x' }, async () => new Response('{"error":"down"}', { status: 503 }));
  const out = await keeping(runSocialTool, broken)({ url: 'https://www.instagram.com/reel/R1/' }, {}, deps(fakeApi({ R1: reelResult('R1', 'bleibt') })));
  assert.equal(out.fullText, 'bleibt');
  assert.match(out.library.error, /Library: down; the text was fetched but not kept/);
});

test('what was fetched before a run fails is still kept', async () => {
  const { library } = await libraryFor('Kunde A');
  const run = keeping(async (_args, _ctx, d) => {
    d.keep({ url: 'https://www.youtube.com/watch?v=a', platform: 'youtube', text: 'erstes Video' });
    throw new Error('YouTube blocked the rest');
  }, library);
  await assert.rejects(run({}), /YouTube blocked the rest \(1 kept in the library\)/);
  assert.equal((await library.search({ q: 'erstes' })).results.length, 1);
});

test('many transcripts go in batches', async () => {
  const sent = [];
  const fake = async (url, init) => {
    const { items } = JSON.parse(init.body);
    sent.push(items.length);
    return new Response(JSON.stringify({ saved: items.map((i, n) => ({ id: n, url: i.url })), rejected: [] }), { status: 200 });
  };
  const records = Array.from({ length: 45 }, (_, n) => ({ url: `https://a.test/${n}`, text: 't' }));
  const out = await libraryClient({ url: 'https://l.test', key: 'k' }, fake).save(records);
  assert.deepEqual(sent, [20, 20, 5]);
  assert.equal(out.saved.length, 45);
});

test('YouTube transcripts of a playlist run are kept one by one', async () => {
  const { library } = await libraryFor('Kunde A');
  const fetcher = async (videoId) => ({ videoId, url: `https://www.youtube.com/watch?v=${videoId}`, title: `Folge ${videoId}`,
    author: 'Kanal', language: 'de', fullText: `Inhalt von ${videoId}`, totalSegments: 1 });
  const run = keeping((_args, _ctx, d) => collectTranscripts(['v1', 'v2'], { route: {}, fetcher, keep: d.keep }), library);
  const out = await run({});
  assert.deepEqual(out.library, { kept: 2 });
  const found = await library.search({ q: 'Inhalt', platform: 'youtube' });
  assert.deepEqual(found.results.map((r) => r.title).sort(), ['Folge v1', 'Folge v2']);
});
