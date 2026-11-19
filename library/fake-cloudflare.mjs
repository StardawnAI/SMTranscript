// Test stand-ins for the Cloudflare runtime: node:sqlite plays a Durable
// Object's SQLite, so schema, FTS5 and triggers run for real; an in-memory
// map plays an R2 bucket, multipart uploads included.
import { createHash, randomUUID } from 'node:crypto';
import { DatabaseSync } from 'node:sqlite';
import { Directory, Downloads, Library } from './worker.mjs';

export function storage() {
  const s = { db: new DatabaseSync(':memory:'), alarm: null };
  s.sql = {
    exec(query, ...bindings) {
      const before = s.db.prepare('select total_changes() as n').get().n;
      const rows = s.db.prepare(query).all(...bindings);
      const rowsWritten = s.db.prepare('select total_changes() as n').get().n - before;
      return { toArray: () => rows, one: () => rows[0], rowsWritten };
    },
  };
  s.transactionSync = (fn) => {
    s.db.exec('begin');
    try {
      const out = fn();
      s.db.exec('commit');
      return out;
    } catch (e) {
      s.db.exec('rollback');
      throw e;
    }
  };
  s.deleteAll = async () => { s.db.close(); s.db = new DatabaseSync(':memory:'); };
  s.setAlarm = async (time) => { s.alarm = Number(time); };
  s.getAlarm = async () => s.alarm;
  s.deleteAlarm = async () => { s.alarm = null; };
  return s;
}

export function namespace(Class, env) {
  const objects = new Map();
  return {
    objects,
    idFromName: (name) => name,
    get: (name) => {
      if (!objects.has(name)) objects.set(name, new Class({ storage: storage() }, env));
      return objects.get(name);
    },
  };
}

// Runs the alarm of every object whose alarm time has come, as Cloudflare does.
export async function fireAlarms(ns) {
  for (const object of ns.objects.values()) {
    const { storage: s } = object.ctx;
    if (s.alarm !== null && s.alarm <= Date.now()) {
      s.alarm = null;
      await object.alarm();
    }
  }
}

const bytesOf = async (value) => new Uint8Array(await new Response(value).arrayBuffer());

// An R2 bucket in memory: put/get (with a range)/head/delete and multipart
// uploads, which, as on R2, join only parts of equal size but the last.
export function bucket() {
  const objects = new Map();
  const uploads = new Map();
  const stored = (key, bytes, httpMetadata = {}) => {
    objects.set(key, { bytes, httpMetadata });
    return { key, size: bytes.length, httpMetadata };
  };
  const upload = (key, uploadId) => ({
    key,
    uploadId,
    async uploadPart(partNumber, value) {
      const u = uploads.get(uploadId);
      if (!u || u.state !== 'open') throw new Error('NoSuchUpload');
      const etag = randomUUID();
      u.parts.set(partNumber, { etag, bytes: await bytesOf(value) });
      return { partNumber, etag };
    },
    async complete(parts) {
      const u = uploads.get(uploadId);
      if (!u || u.state !== 'open') throw new Error('NoSuchUpload');
      const chunks = parts.map((p) => {
        const part = u.parts.get(p.partNumber);
        if (!part || part.etag !== p.etag) throw new Error(`part ${p.partNumber} is not uploaded`);
        return part.bytes;
      });
      if (chunks.slice(0, -1).some((c) => c.length !== chunks[0].length)) throw new Error('parts are not of equal size');
      const bytes = new Uint8Array(chunks.reduce((n, c) => n + c.length, 0));
      let at = 0;
      for (const c of chunks) { bytes.set(c, at); at += c.length; }
      u.state = 'completed';
      u.parts.clear();
      return stored(key, bytes, u.httpMetadata);
    },
    async abort() {
      const u = uploads.get(uploadId);
      if (!u || u.state !== 'open') throw new Error('NoSuchUpload');
      u.state = 'aborted';
      u.parts.clear();
    },
  });
  return {
    objects,
    uploads,
    async put(key, value, options = {}) { return stored(key, await bytesOf(value), options.httpMetadata); },
    async head(key) {
      const o = objects.get(key);
      return o ? { key, size: o.bytes.length, httpMetadata: o.httpMetadata } : null;
    },
    async get(key, options = {}) {
      const o = objects.get(key);
      if (!o) return null;
      const r = options.range;
      const bytes = r ? o.bytes.slice(r.offset, r.offset + r.length) : o.bytes;
      return { key, size: o.bytes.length, httpMetadata: o.httpMetadata, range: r, body: new Response(bytes).body };
    },
    async delete(key) { objects.delete(key); },
    async createMultipartUpload(key, options = {}) {
      const uploadId = randomUUID();
      uploads.set(uploadId, { key, state: 'open', parts: new Map(), httpMetadata: options.httpMetadata || {} });
      return upload(key, uploadId);
    },
    resumeMultipartUpload: (key, uploadId) => upload(key, uploadId),
  };
}

const sha = (token) => (token ? createHash('sha256').update(token).digest('hex') : '');

export function cloudflareEnv(vars = {}) {
  // ADMIN_TOKEN and UPLOAD_TOKEN are the tests' copies of the tokens; the worker reads only their hashes.
  const token = vars.ADMIN_TOKEN ?? 'a'.repeat(40);
  const upload = vars.UPLOAD_TOKEN ?? 'u'.repeat(40);
  const env = { ADMIN_TOKEN: token, ADMIN_TOKEN_SHA256: sha(token), UPLOAD_TOKEN: upload, UPLOAD_TOKEN_SHA256: sha(upload), ...vars };
  env.LIBRARY = namespace(Library, env);
  env.DIRECTORY = namespace(Directory, env);
  env.DOWNLOADS = bucket();
  env.DOWNLOAD_INDEX = namespace(Downloads, env);
  return env;
}
