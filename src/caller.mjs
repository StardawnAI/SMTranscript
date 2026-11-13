// Who a call is for. A hub that serves many people with one SMTranscript (the
// AWG hub) names the person on every tools/call in _meta["stardawn/caller"];
// the hub sets it itself, a client cannot. That person's library is used, and
// the worker's paid transcription is charged to that person's daily share.
// A call without one (this server on one person's own computer) is nobody's.

import { createHash } from 'node:crypto';

const sha256 = (text) => createHash('sha256').update(text).digest('hex');

/** The caller named in a tools/call, or null. */
export function callerOf(params) {
  const c = params?._meta?.['stardawn/caller'];
  const id = typeof c?.id === 'string' ? c.id.trim() : '';
  if (!id || id.length > 200) return null;
  return { id, ...(typeof c.name === 'string' && c.name.trim() ? { name: c.name.trim().slice(0, 200) } : {}) };
}

/** The caller as the worker's member: letters, digits and : _ . @ - only. */
export function memberOf(caller) {
  if (!caller) return null;
  return /^[A-Za-z0-9][A-Za-z0-9_.@-]{0,100}$/.test(caller.id) ? `user:${caller.id}` : `user:${sha256(caller.id).slice(0, 32)}`;
}

/** The caller's library: one named for them in `members`, or one of their own. */
export function libraryIdOf(caller, members = {}) {
  if (!caller) return null;
  const named = members[caller.id];
  if (typeof named === 'string' && /^[a-z0-9]{16}$/.test(named)) return named;
  return sha256(`awg-member:${caller.id}`).slice(0, 16);
}
