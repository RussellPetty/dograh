'use client';

/**
 * Embed auth bridge for the "Viato Voice" deployment.
 *
 * When the Viato Voice UI is embedded inside Viato CRM, the parent renders it in
 * an iframe with `?embed=clerk` (and `?parentOrigin=<crm-origin>`) and posts the
 * current Clerk JWT in via `window.postMessage`. This module receives/refreshes
 * that token and exposes it so the API client can attach
 * `Authorization: Bearer <jwt>` to every request (the backend verifies it against
 * Clerk's JWKS — see api/services/auth/clerk_auth.py).
 *
 * Standalone (non-embedded) loads never set `?embed=clerk`, so the bridge is a
 * no-op and `getToken()` resolves to null. The postMessage origin check is
 * defense-in-depth; real security is the server-side JWKS verification.
 *
 * Ported from the Presenton embed bridge (servers/nextjs/utils/clerkToken.ts).
 */

const EMBED_FLAG_KEY = 'viato_voice_embed_clerk';
const PARENT_ORIGIN_KEY = 'viato_voice_parent_origin';

const MSG_READY = 'viato-voice-ready';
const MSG_TOKEN_REQUEST = 'viato-voice-token-request';
const MSG_AUTH = 'viato-voice-auth';

let _token: string | null = null;
let _expEpochMs: number | null = null;
let _initialized = false;
let _refreshTimer: ReturnType<typeof setTimeout> | null = null;
const _waiters: Array<(t: string | null) => void> = [];

function readSearchParam(name: string): string | null {
  if (typeof window === 'undefined') return null;
  try {
    return new URLSearchParams(window.location.search).get(name);
  } catch {
    return null;
  }
}

/** True when running embedded under Clerk auth (sticky across reloads). */
export function isEmbedClerkMode(): boolean {
  if (typeof window === 'undefined') return false;
  if (readSearchParam('embed') === 'clerk') {
    try {
      window.sessionStorage.setItem(EMBED_FLAG_KEY, '1');
      const po = readSearchParam('parentOrigin');
      if (po) window.sessionStorage.setItem(PARENT_ORIGIN_KEY, po);
    } catch {
      /* sessionStorage may be unavailable; URL param still drives this load */
    }
    return true;
  }
  try {
    return window.sessionStorage.getItem(EMBED_FLAG_KEY) === '1';
  } catch {
    return false;
  }
}

function parentOrigin(): string | null {
  const fromUrl = readSearchParam('parentOrigin');
  if (fromUrl) return fromUrl;
  try {
    return window.sessionStorage.getItem(PARENT_ORIGIN_KEY);
  } catch {
    return null;
  }
}

function decodePayload(jwt: string): Record<string, unknown> | null {
  try {
    const part = jwt.split('.')[1].replace(/-/g, '+').replace(/_/g, '/');
    return JSON.parse(atob(part));
  } catch {
    return null;
  }
}

/** Decode (without verifying) the JWT claims — used only to populate the local user object. */
export function decodeClaims(jwt: string): Record<string, unknown> | null {
  return decodePayload(jwt);
}

function decodeExpEpochMs(jwt: string): number | null {
  const payload = decodePayload(jwt);
  return payload && typeof payload.exp === 'number' ? payload.exp * 1000 : null;
}

function tokenValid(): boolean {
  return !!_token && (_expEpochMs == null || _expEpochMs - Date.now() > 5000);
}

function postToParent(message: unknown) {
  if (typeof window === 'undefined' || window.parent === window) return;
  try {
    window.parent.postMessage(message, parentOrigin() || '*');
  } catch {
    /* ignore */
  }
}

function scheduleRefresh() {
  if (typeof window === 'undefined' || _expEpochMs == null) return;
  if (_refreshTimer) clearTimeout(_refreshTimer);
  const delay = Math.max(5000, _expEpochMs - Date.now() - 60000); // ~60s before expiry
  _refreshTimer = setTimeout(() => postToParent({ type: MSG_TOKEN_REQUEST }), delay);
}

function setToken(token: string, expiresAtEpochSec?: number) {
  _token = token;
  _expEpochMs = expiresAtEpochSec ? expiresAtEpochSec * 1000 : decodeExpEpochMs(token);
  while (_waiters.length) {
    const w = _waiters.shift();
    if (w) w(token);
  }
  scheduleRefresh();
}

/** Start listening for the parent's Clerk token. No-op when not embedded. */
export function initEmbedBridge() {
  if (_initialized || typeof window === 'undefined') return;
  _initialized = true;
  if (!isEmbedClerkMode()) return;

  window.addEventListener('message', (event: MessageEvent) => {
    const expected = parentOrigin();
    if (expected && event.origin !== expected) return; // origin allow-list
    if (event.source && event.source !== window.parent) return; // only the embedding parent
    const data = event.data as { type?: string; token?: unknown; expiresAt?: unknown } | null;
    if (!data || typeof data !== 'object') return;
    if (data.type === MSG_AUTH && typeof data.token === 'string') {
      setToken(data.token, typeof data.expiresAt === 'number' ? data.expiresAt : undefined);
    }
  });

  postToParent({ type: MSG_READY }); // ask the parent to send the token
}

/** Current token if valid, else null (for cases that can't await). */
export function getTokenSync(): string | null {
  return tokenValid() ? _token : null;
}

/**
 * Resolve the current Clerk token. Returns null immediately when not embedded
 * (standalone) so callers attach no Authorization header. When embedded, returns
 * a valid token, waiting (bounded) for the parent to deliver one.
 */
export async function getToken(): Promise<string | null> {
  if (typeof window === 'undefined' || !isEmbedClerkMode()) return null;
  if (tokenValid()) return _token;
  postToParent({ type: MSG_TOKEN_REQUEST });
  return new Promise<string | null>((resolve) => {
    let settled = false;
    const done = (t: string | null) => {
      if (!settled) {
        settled = true;
        resolve(t);
      }
    };
    _waiters.push(done);
    setTimeout(() => done(_token), 8000); // don't hang forever if the parent is silent
  });
}
