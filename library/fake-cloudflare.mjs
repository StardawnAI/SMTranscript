// Test stand-ins for the Cloudflare runtime: node:sqlite plays a Durable
// Object's SQLite, so schema, FTS5 and triggers run for real.
import { createHash } from 'node:crypto';
import { DatabaseSync } from 'node:sqlite';
import { Directory, Library } from './worker.mjs';

export function storage() {
  const s = { db: new DatabaseSync(':memory:') };
  s.sql = {
    exec(query, ...bindings) {
      const rows = s.db.prepare(query).all(...bindings);
      return { toArray: () => rows, one: () => rows[0] };
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
  return s;
}

export function namespace(Class, env) {
  const objects = new Map();
  return {
    idFromName: (name) => name,
    get: (name) => {
      if (!objects.has(name)) objects.set(name, new Class({ storage: storage() }, env));
      return objects.get(name);
    },
  };
}

export function cloudflareEnv(vars = {}) {
  // ADMIN_TOKEN is the tests' copy of the token; the worker reads only its hash.
  const token = vars.ADMIN_TOKEN ?? 'a'.repeat(40);
  const env = { ADMIN_TOKEN: token, ADMIN_TOKEN_SHA256: token ? createHash('sha256').update(token).digest('hex') : '', ...vars };
  env.LIBRARY = namespace(Library, env);
  env.DIRECTORY = namespace(Directory, env);
  return env;
}
