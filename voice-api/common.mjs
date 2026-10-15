export class ApiError extends Error { constructor(status, message) { super(message); this.status = status; } }
export const json = (body, status = 200) => Response.json(body, { status, headers: { 'Cache-Control': 'no-store' } });
export const text = value => typeof value === 'string' ? value.trim() : '';
export const uuid = value => typeof value === 'string' && /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(value);
export const hex = buffer => [...new Uint8Array(buffer)].map(n => n.toString(16).padStart(2, '0')).join('');
export async function digest(value) { return hex(await crypto.subtle.digest('SHA-256', new TextEncoder().encode(value))); }
export async function stableUuid(value) {
  const h = await digest(value);
  return `${h.slice(0,8)}-${h.slice(8,12)}-5${h.slice(13,16)}-a${h.slice(17,20)}-${h.slice(20,32)}`;
}
export function sameSecret(a, b) {
  if (!a || !b || a.length !== b.length) return false;
  let result = 0;
  for (let i = 0; i < a.length; i++) result |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return result === 0;
}
export function runtime(options = {}) { return { env: options.env || process.env, fetch: options.fetch || globalThis.fetch, now: options.now || Date.now }; }
export async function requestJson(req) {
  let data;
  try { data = await req.json(); } catch { throw new ApiError(400, 'invalid JSON body'); }
  if (!data || Array.isArray(data) || typeof data !== 'object') throw new ApiError(400, 'JSON object required');
  return data;
}
export async function supabase(ctx, path, init = {}) {
  const url = ctx.env.SUPABASE_URL || 'https://lyvhrxiukmlvrznkrtkv.supabase.co';
  const key = ctx.env.SUPABASE_SERVICE_ROLE_KEY;
  if (!key) throw new ApiError(503, 'CRM is not configured');
  const response = await ctx.fetch(`${url}/rest/v1/${path}`, { ...init,
    headers: { apikey: key, Authorization: `Bearer ${key}`, 'Content-Type': 'application/json', ...init.headers }, signal: AbortSignal.timeout(10000) });
  if (!response.ok) throw new ApiError(502, 'CRM request failed');
  const body = await response.text();
  return body ? JSON.parse(body) : null;
}
export function failure(error) { return json({ error: error instanceof ApiError ? error.message : 'upstream request failed' }, error instanceof ApiError ? error.status : 502); }
