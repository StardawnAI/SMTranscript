// Network-free fixtures contain at most 13 media bytes; test output is captured by the runner.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import worker, { Budget, validateSource, transcribe } from './worker.mjs';

const source = () => ({ url: 'https://video.cdninstagram.com/test.mp4', duration: 30,
  ext: 'mp4', headers: { 'User-Agent': 'SMTranscript' } });
const env = { RELAY_TOKEN: 'test-secret', DAILY_EUR_LIMIT: '0.01' };
function storage() {
  const values = new Map();
  let tail = Promise.resolve();
  const s = { get: async k => values.get(k), put: async (k, v) => values.set(k, v),
    transaction: f => { const next = tail.then(() => f(s)); tail = next.catch(() => {}); return next; } };
  return s;
}
test('unauthenticated requests cannot reach a source or provider', async () => {
  const r = await worker.fetch(new Request('https://relay.test/v1/transcribe', { method: 'POST' }), env);
  assert.equal(r.status, 401);
});
test('only HTTPS media CDN URLs and scoped headers are accepted', () => {
  assert.equal(validateSource(source()).ext, 'mp4');
  assert.equal(validateSource({ ...source(), url: 'https://v16-webapp-prime.tiktok.com/video' }).ext, 'mp4');
  for (const url of ['http://video.cdninstagram.com/a', 'https://127.0.0.1/a',
    'https://cdninstagram.com.attacker.test/a', 'https://x@video.cdninstagram.com/a']) {
    assert.throws(() => validateSource({ ...source(), url }));
  }
  assert.throws(() => validateSource({ ...source(), duration: 0 }));
  assert.throws(() => validateSource({ ...source(), headers: { Authorization: 'secret' } }));
});
test('streamed upload contains media and timed transcription options', async () => {
  const calls = [];
  const result = await transcribe(source(), 'provider-secret', async (url, opts) => {
    calls.push(url);
    if (calls.length === 1) return new Response('audio-payload', { headers: { 'Content-Length': '13' } });
    assert.equal(opts.headers.Authorization, 'Bearer provider-secret');
    const body = await new Response(opts.body).text();
    assert.ok(body.length < 2048);
    assert.match(body, /audio-payload/);
    assert.match(body, /whisper-large-v3-turbo/);
    assert.match(body, /verbose_json/);
    return Response.json({ text: 'Hello world', duration: 30, language: 'english',
      segments: [{ start: 0, end: 2, text: 'Hello world' }] });
  });
  assert.equal(calls.length, 2);
  assert.equal(result.media_bytes, 13);
  assert.equal(result.segments[0].end_ms, 2000);
});
test('large and unknown-length media never reach the paid provider', async () => {
  for (const headers of [{}, { 'Content-Length': String(26 * 1024 * 1024) }]) {
    let calls = 0;
    await assert.rejects(transcribe(source(), 'key', async () => {
      calls++; return new Response('x', { headers });
    }));
    assert.equal(calls, 1);
  }
});
test('redirects cannot escape the CDN allowlist or forward session cookies', async () => {
  let calls = 0;
  await assert.rejects(transcribe(source(), 'key', async () => {
    calls++; return new Response(null, { status: 302, headers: { Location: 'https://localhost/private' } });
  }));
  assert.equal(calls, 1);
});
test('concurrent budget reservations enforce a euro ceiling and prevent replay', async () => {
  const b = new Budget({ storage: storage() }, env);
  const reserve = id => b.fetch(new Request('https://budget/reserve', { method: 'POST',
    body: JSON.stringify({ id, duration: 300 }) }));
  const results = await Promise.all(Array.from({ length: 10 }, (_, i) => reserve(`job-${i}`)));
  assert.equal(results.filter(r => r.status === 200).length, 1);
  assert.equal((await reserve('job-0')).status, 409);
  const a = await results.find(r => r.status === 200).json();
  assert.equal(a.budget_alert, true);
});
test('completed results are replayed without a second reservation', async () => {
  const b = new Budget({ storage: storage() }, env);
  const call = (path, body) => b.fetch(new Request(`https://budget/${path}`, {
    method: 'POST', body: JSON.stringify(body) }));
  await call('reserve', { id: 'stable-id', duration: 30 });
  await call('complete', { id: 'stable-id', result: { text: 'Done' } });
  assert.deepEqual((await (await call('reserve', { id: 'stable-id', duration: 30 })).json()).result, { text: 'Done' });
});

test('a definite pre-provider failure permits a bounded retry with the same reservation', async () => {
  const b = new Budget({ storage: storage() }, env);
  const call = (path, body) => b.fetch(new Request(`https://budget/${path}`, {
    method: 'POST', body: JSON.stringify(body) }));
  const first = await (await call('reserve', { id: 'retry-id', duration: 30 })).json();
  for (let attempt = 1; attempt < 3; attempt++) {
    await call('source_failed', { id: 'retry-id' });
    const retried = await call('reserve', { id: 'retry-id', duration: 30 });
    assert.equal(retried.status, 200);
    assert.equal((await retried.json()).reserved_eur, first.reserved_eur);
  }
  await call('source_failed', { id: 'retry-id' });
  assert.equal((await call('reserve', { id: 'retry-id', duration: 30 })).status, 409);
});

test('failed source requests still expose the half-budget alert', async () => {
  const b = new Budget({ storage: storage() }, { DAILY_EUR_LIMIT: '0.002' });
  const binding = { idFromName: () => 'global', get: () => ({
    fetch: (url, options) => b.fetch(new Request(url, options)) }) };
  const previous = globalThis.fetch;
  globalThis.fetch = async () => new Response(null, { status: 403 });
  try {
    const r = await worker.fetch(new Request('https://relay.test/v1/transcribe', {
      method: 'POST', headers: { Authorization: 'Bearer test-secret', 'X-Groq-Key': 'gsk_test' },
      body: JSON.stringify({ id: 'a'.repeat(64), source: source() }) }), { ...env, BUDGET: binding });
    assert.equal(r.status, 422);
    assert.equal((await r.json()).budget_alert, true);
  } finally { globalThis.fetch = previous; }
});
