// Media flows through streams only. Durable storage contains budget records and transcripts.
const MAX_BYTES = 25 * 1024 * 1024;
const MAX_SECONDS = 900;
const MODEL = 'whisper-large-v3-turbo';
const CDN_HOSTS = ['cdninstagram.com', 'fbcdn.net', 'tiktokcdn.com', 'tiktokcdn-us.com',
  'tiktokcdn-eu.com', 'tiktokv.com', 'byteoversea.com', 'ibytedtos.com', 'muscdn.com', 'vimeocdn.com'];
const encoder = new TextEncoder();
const reply = (data, status = 200) => Response.json(data, { status, headers: { 'Cache-Control': 'no-store' } });
const fail = (message, status = 400) => Object.assign(new Error(message), { status });

export function validateSource(source) {
  const url = new URL(source.url);
  if (url.protocol !== 'https:' || url.username || url.password || (url.port && url.port !== '443') ||
      !(CDN_HOSTS.some(host => url.hostname === host || url.hostname.endsWith(`.${host}`)) ||
        /^v[0-9]+(?:-[a-z0-9]+)+\.tiktok\.com$/.test(url.hostname))) {
    throw fail('Source must be an approved HTTPS media CDN.');
  }
  if (!Number.isFinite(source.duration) || source.duration <= 0 || source.duration > MAX_SECONDS) {
    throw fail(`A verified duration between 0 and ${MAX_SECONDS} seconds is required.`);
  }
  if (!['mp4', 'm4a', 'mp3', 'wav', 'webm', 'ogg', 'flac'].includes(source.ext)) {
    throw fail('Source format is unsupported.');
  }
  for (const [name, value] of Object.entries(source.headers || {})) {
    if (!['user-agent', 'referer', 'cookie', 'accept', 'accept-language'].includes(name.toLowerCase()) ||
        typeof value !== 'string' || value.length > 8192 || /[\r\n]/.test(value)) {
      throw fail('Source headers are unsupported.');
    }
  }
  return source;
}

async function boundedJson(response, max = 512 * 1024) {
  const reader = response.body.getReader();
  const chunks = [];
  let bytes = 0;
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    bytes += value.byteLength;
    if (bytes > max) { await reader.cancel(); throw fail('JSON response is too large.'); }
    chunks.push(value);
  }
  const data = new Uint8Array(bytes);
  let offset = 0;
  for (const chunk of chunks) { data.set(chunk, offset); offset += chunk.byteLength; }
  return JSON.parse(new TextDecoder().decode(data));
}

async function mediaResponse(source, fetcher, signal) {
  let url = source.url;
  for (let hop = 0; hop < 4; hop++) {
    validateSource({ ...source, url });
    // Session cookies never cross a redirect to a different hostname.
    const headers = new Headers(source.headers);
    if (new URL(url).hostname !== new URL(source.url).hostname) headers.delete('cookie');
    headers.set('Accept-Encoding', 'identity');
    const response = await fetcher(url, { headers, redirect: 'manual', signal, cache: 'no-store' });
    if ([301, 302, 303, 307, 308].includes(response.status)) {
      await response.body?.cancel();
      url = new URL(response.headers.get('Location'), url).href;
      continue;
    }
    if (response.status !== 200) {
      await response.body?.cancel();
      throw fail(`Media source returned HTTP ${response.status}.`, 422);
    }
    const size = Number(response.headers.get('Content-Length'));
    if (!Number.isSafeInteger(size) || size <= 0 || size > MAX_BYTES ||
        (response.headers.get('Content-Encoding') || 'identity') !== 'identity') {
      await response.body?.cancel();
      throw fail('Media needs a known size of at most 25 MiB and identity encoding.', 413);
    }
    return { response, size };
  }
  throw fail('Too many media redirects.', 422);
}

function multipart(media, size, ext, boundary, language) {
  const fields = { model: MODEL, response_format: 'verbose_json', 'timestamp_granularities[]': 'segment' };
  if (language) fields.language = language;
  const prefix = encoder.encode(Object.entries(fields).map(([key, value]) =>
    `--${boundary}\r\nContent-Disposition: form-data; name="${key}"\r\n\r\n${value}\r\n`).join('') +
    `--${boundary}\r\nContent-Disposition: form-data; name="file"; filename="media.${ext}"\r\nContent-Type: application/octet-stream\r\n\r\n`);
  const suffix = encoder.encode(`\r\n--${boundary}--\r\n`);
  const reader = media.getReader();
  let first = true, transferred = 0;
  return new ReadableStream({
    async pull(controller) {
      if (first) { first = false; controller.enqueue(prefix); return; }
      const { value, done } = await reader.read();
      if (done) {
        if (transferred !== size) { controller.error(fail('Media length mismatch.')); return; }
        controller.enqueue(suffix); controller.close(); return;
      }
      transferred += value.byteLength;
      if (transferred > size) { await reader.cancel(); controller.error(fail('Media exceeds declared size.')); return; }
      controller.enqueue(value);
    },
    cancel(reason) { return reader.cancel(reason); },
  });
}

export async function transcribe(source, key, fetcher = fetch, language = null) {
  validateSource(source);
  const abort = new AbortController();
  const timer = setTimeout(() => abort.abort(), 120000);
  let providerStarted = false;
  try {
    const { response, size } = await mediaResponse(source, fetcher, abort.signal);
    const boundary = `smt-${crypto.randomUUID()}`;
    // paid-call: Budget reserves a conservative euro amount atomically before this call.
    providerStarted = true;
    const result = await fetcher('https://api.groq.com/openai/v1/audio/transcriptions', {
      method: 'POST', headers: { Authorization: `Bearer ${key}`,
        'Content-Type': `multipart/form-data; boundary=${boundary}` },
      body: multipart(response.body, size, source.ext, boundary, language),
      signal: abort.signal, duplex: 'half',
    });
    if (!result.ok) { await result.body?.cancel(); throw fail(`Groq returned HTTP ${result.status}.`, 502); }
    const data = await boundedJson(result);
    if (!Array.isArray(data.segments) || typeof data.text !== 'string') throw fail('Invalid Groq transcript.', 502);
    return { text: data.text.trim(), language: data.language || null, model: `groq:${MODEL}`,
      duration_seconds: data.duration, media_bytes: size, transport: 'cloudflare-stream',
      segments: data.segments.map(s => ({ start_ms: Math.round(s.start * 1000),
        end_ms: Math.round(s.end * 1000), text: s.text.trim() })) };
  } catch (error) {
    error.providerStarted = providerStarted;
    throw error;
  } finally { clearTimeout(timer); abort.abort(); }
}

export class Budget {
  constructor(state, env) { this.storage = state.storage; this.env = env; }
  async fetch(request) {
    const input = await request.json();
    const jobKey = `job:${input.id}`;
    return this.storage.transaction(async tx => {
      const job = await tx.get(jobKey);
      if (new URL(request.url).pathname === '/source_failed') {
        if (!job || job.result) return reply({ error: 'Unknown or completed reservation.' }, 409);
        await tx.put(jobKey, { ...job, source_failed: true });
        return reply({ ok: true });
      }
      if (new URL(request.url).pathname === '/complete') {
        if (!job) return reply({ error: 'Unknown reservation.' }, 409);
        await tx.put(jobKey, { ...job, result: input.result });
        return reply({ ok: true });
      }
      if (job) {
        if (job.result) return reply({ result: job.result });
        if (job.source_failed && job.attempts < 3 && job.day === new Date().toISOString().slice(0, 10)) {
          await tx.put(jobKey, { ...job, source_failed: false, attempts: job.attempts + 1 });
          return reply({ reserved_eur: job.reserved / 1e6, budget_alert: job.budget_alert });
        }
        return reply({ error: 'Already reserved; an uncertain provider call or exhausted source retry is not repeated.',
          budget_alert: job.budget_alert || false }, 409);
      }
      const day = new Date().toISOString().slice(0, 10);
      const limit = Number(this.env.DAILY_EUR_LIMIT || '0.25');
      if (!(limit > 0 && limit <= 1) || !(input.duration > 0 && input.duration <= MAX_SECONDS)) {
        return reply({ error: 'Invalid budget configuration or duration.' }, 503);
      }
      // $0.04/hour, >=10 seconds. Reserve 2 EUR/USD and a 30-second duration margin.
      // Reserves are never refunded, including failures. Only trusted resolver metadata is accepted.
      const reserved = Math.ceil(Math.max(input.duration + 30, 10) * 0.04 / 3600 * 2 * 1e6);
      const spent = (await tx.get(`day:${day}`) || 0) + reserved;
      if (spent > Math.floor(limit * 1e6)) return reply({ error: 'Daily transcription budget reached.', budget_alert: true }, 429);
      const budget_alert = spent >= limit * 1e6 / 2;
      await tx.put(`day:${day}`, spent);
      await tx.put(jobKey, { reserved, day, attempts: 1, budget_alert });
      return reply({ reserved_eur: reserved / 1e6, budget_alert });
    });
  }
}

export default {
  async fetch(request, env) {
    if (!env.RELAY_TOKEN || request.headers.get('Authorization') !== `Bearer ${env.RELAY_TOKEN}`) {
      return reply({ error: 'Unauthorized.' }, 401);
    }
    const path = new URL(request.url).pathname;
    if (path === '/healthz' && request.method === 'GET') return reply({ ok: true, media_storage: false });
    if (path !== '/v1/transcribe' || request.method !== 'POST') return reply({ error: 'Not found.' }, 404);
    let budgetAlert = false;
    try {
      const input = await boundedJson(request, 24 * 1024);
      const source = validateSource(input.source);
      if (!/^[a-f0-9]{64}$/.test(input.id || '') || !/^gsk_[A-Za-z0-9]+$/.test(request.headers.get('X-Groq-Key') || '') ||
          (input.language && !/^[a-z]{2,3}$/.test(input.language))) throw fail('Invalid job metadata.');
      const budget = env.BUDGET.get(env.BUDGET.idFromName('global'));
      const call = (path, body) => budget.fetch(`https://budget/${path}`, {
        method: 'POST', body: JSON.stringify(body) });
      const reservation = await call('reserve', { id: input.id, duration: source.duration });
      const record = await reservation.json();
      budgetAlert = record.budget_alert || false;
      if (!reservation.ok) return reply(record, reservation.status);
      if (record.result) return reply({ ...record.result, cached: true });
      let result;
      try {
        result = await transcribe(source, request.headers.get('X-Groq-Key'), fetch, input.language);
      } catch (error) {
        if (error.providerStarted === false) await call('source_failed', { id: input.id });
        throw error;
      }
      result.budget_alert = record.budget_alert;
      result.reserved_eur = record.reserved_eur;
      await call('complete', { id: input.id, result });
      return reply(result);
    } catch (error) {
      return reply({ error: error.status ? error.message : 'Relay request failed; no automatic paid retry.',
        budget_alert: budgetAlert }, error.status || 502);
    }
  },
};
