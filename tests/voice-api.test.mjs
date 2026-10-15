import test from 'node:test';
import assert from 'node:assert/strict';
import { createHmac } from 'node:crypto';
import { handleCalendar, validDate, zonedDateTime } from '../voice-api/calendar.mjs';
import { handlePostCall, verifySignature } from '../voice-api/post-call.mjs';
import { supabase } from '../voice-api/common.mjs';

const now = Date.parse('2026-10-14T12:00:00Z');
const env = { VOICE_API_SECRET: 'test-secret', GOOGLE_CLIENT_ID: 'test-id', GOOGLE_CLIENT_SECRET: 'test-google-secret', GOOGLE_REFRESH_TOKEN: 'test-refresh', RETELL_WEBHOOK_SECRET: 'test-webhook', SUPABASE_SERVICE_ROLE_KEY: 'test-service-key' };
const leadId = '11111111-1111-4111-8111-111111111111';
const response = (data, status = 200) => status === 204 ? new Response(null, { status }) : Response.json(data, { status });
const calendarReq = body => new Request('https://example.test/calendar', { method: 'POST', headers: { 'x-voice-secret': env.VOICE_API_SECRET }, body: JSON.stringify(body) });
const sign = (raw, stamp = now) => `v=${stamp},d=${createHmac('sha256', env.RETELL_WEBHOOK_SECRET).update(raw + stamp).digest('hex')}`;
const webhookReq = body => { const raw = JSON.stringify(body); return new Request('https://example.test/post-call', { method: 'POST', headers: { 'x-retell-signature': sign(raw) }, body: raw }); };

test('calendar rejects missing configuration, bad secrets, invalid dates/times, and missing phone before network access', async () => {
  const network = () => { throw new Error('network must not be called'); };
  assert.equal((await handleCalendar(calendarReq({}), { env: {}, fetch: network })).status, 503);
  assert.equal((await handleCalendar(new Request('https://example.test', { method: 'POST', body: '{}' }), { env, fetch: network })).status, 401);
  for (const body of [{ action: 'check_availability', date: '2026-02-30' }, { action: 'book_appointment', date: '2026-10-15', time: '25:00', lead_name: 'Test', lead_phone: '2025550100' }, { action: 'book_appointment', date: '2026-10-15', time: '10:00', lead_name: 'Test' }]) {
    assert.equal((await handleCalendar(calendarReq(body), { env, fetch: network })).status, 400);
  }
  assert.equal(validDate('2028-02-29'), true);
  assert.equal(zonedDateTime('2026-10-15', '09:00'), '2026-10-15T09:00:00-04:00');
  assert.equal(zonedDateTime('2026-11-02', '09:00'), '2026-11-02T09:00:00-05:00');
});
test('availability uses UTC busy periods, excludes past slots, and accepts the Retell envelope', async () => {
  const calls = [];
  const fetch = async (url, init) => { calls.push([url, init]); return url.includes('/token') ? response({ access_token: 'test-access' }) : response({ calendars: { primary: { busy: [{ start: '2026-10-14T14:00:00Z', end: '2026-10-14T15:00:00Z' }] } } }); };
  const result = await handleCalendar(calendarReq({ name: 'check_availability', args: { date: '2026-10-14' } }), { env, fetch, now: () => Date.parse('2026-10-14T13:45:00Z') });
  const data = await result.json();
  assert.equal(result.status, 200);
  assert.equal(data.slots[0], '11:00');
  assert(!data.slots.includes('09:30'));
  assert(!data.slots.includes('10:00'));
  assert(!data.slots.includes('10:30'));
  assert.equal(data.duration_minutes, 30);
  assert.equal(JSON.parse(calls[1][1].body).timeMin, '2026-10-14T09:00:00-04:00');
});
test('all-day busy and Google per-calendar errors never report free availability', async () => {
  for (const calendar of [{ busy: [{ start: '2026-10-15T00:00:00-04:00', end: '2026-10-16T00:00:00-04:00' }] }, { errors: [{ reason: 'notFound' }] }]) {
    const fetch = async url => url.includes('/token') ? response({ access_token: 'test-access' }) : response({ calendars: { primary: calendar } });
    const result = await handleCalendar(calendarReq({ action: 'check_availability', date: '2026-10-15' }), { env, fetch, now: () => now });
    if (calendar.busy) assert.deepEqual((await result.json()).slots, []); else assert.equal(result.status, 502);
  }
});
test('booking rechecks conflicts and never inserts a busy slot', async () => {
  let inserted = false;
  const fetch = async (url, init) => {
    if (url.includes('/token')) return response({ access_token: 'test-access' });
    if (url.includes('/events/')) return response({}, 404);
    if (url.endsWith('/freeBusy')) return response({ calendars: { primary: { busy: [{ start: '2026-10-15T13:00:00Z', end: '2026-10-15T14:00:00Z' }] } } });
    inserted = true; return response({});
  };
  const result = await handleCalendar(calendarReq({ action: 'book_appointment', date: '2026-10-15', time: '09:00', lead_name: 'Synthetic Test', lead_phone: '2025550100' }), { env, fetch, now: () => now });
  assert.equal(result.status, 409); assert.equal(inserted, false);
});
test('booking creates one 30-minute event and retries return that event without duplication', async () => {
  let event; let creates = 0;
  const fetch = async (url, init) => {
    if (url.includes('/token')) return response({ access_token: 'test-access' });
    if (url.includes('/events/')) return event ? response(event) : response({}, 404);
    if (url.endsWith('/freeBusy')) return response({ calendars: { primary: { busy: [] } } });
    if (url.endsWith('/events') && init.method === 'POST') { event = JSON.parse(init.body); creates++; return response(event); }
    throw new Error('unexpected network request');
  };
  const body = { name: 'book_appointment', args: { date: '2026-10-15', time: '10:00', lead_name: 'Synthetic Test', lead_phone: '2025550100', lead_id: '' } };
  for (let i = 0; i < 2; i++) { const result = await handleCalendar(calendarReq(body), { env, fetch, now: () => now }); assert.equal(result.status, 200); assert.equal((await result.json()).booked, true); }
  assert.equal(creates, 1);
  assert.equal(Date.parse(event.end.dateTime) - Date.parse(event.start.dateTime), 30 * 60000);
  assert.equal(event.start.timeZone, 'America/New_York');
});
test('Retell signature verification rejects modified bodies, expired signatures, and bare digests', async () => {
  const raw = '{ "event": "call_analyzed" }';
  assert.equal(await verifySignature(raw, sign(raw), env.RETELL_WEBHOOK_SECRET, now), true);
  assert.equal(await verifySignature(raw + ' ', sign(raw), env.RETELL_WEBHOOK_SECRET, now), false);
  assert.equal(await verifySignature(raw, sign(raw, now - 300001), env.RETELL_WEBHOOK_SECRET, now), false);
  assert.equal(await verifySignature(raw, sign(raw).split('d=')[1], env.RETELL_WEBHOOK_SECRET, now), false);
});
test('webhook reads nested analysis/disposition, retains the full transcript and deduplicates records atomically', async () => {
  const rows = new Map(); const updates = [];
  const fetch = async (url, init = {}) => {
    if (url.includes('/leads?') && !init.method) return response([{ id: leadId }]);
    if (url.includes('/activity_log?')) { const row = JSON.parse(init.body); assert.match(init.headers.Prefer, /ignore-duplicates/); rows.set(row.id, row); return response(null, 204); }
    updates.push(JSON.parse(init.body)); return response(null, 204);
  };
  const transcript = 'Synthetic test transcript '.repeat(400);
  const body = { event: 'call_analyzed', call: { call_id: 'synthetic-call', metadata: { lead_id: leadId }, duration_ms: 31000, transcript, call_analysis: { call_summary: 'Synthetic summary', user_sentiment: 'Negative', custom_analysis_data: { disposition: 'do_not_call' } } } };
  for (let i = 0; i < 2; i++) assert.equal((await handlePostCall(webhookReq(body), { env, fetch, now: () => now })).status, 200);
  assert.equal(rows.size, 1);
  const row = [...rows.values()][0];
  assert.equal(row.outcome, 'Do Not Call'); assert(row.notes.includes(transcript)); assert(row.notes.includes('Synthetic summary')); assert(row.notes.includes('Duration: 31s'));
  assert.equal(updates[0].do_not_call, true); assert.equal(updates[0].dnc_reason, 'requested'); assert(!('last_touch' in updates[0]));
});
test('unmatched calls are retained, and call_ended is ignored', async () => {
  let row;
  const fetch = async (url, init) => { if (url.includes('/leads?')) return response([]); row = JSON.parse(init.body); return response(null, 204); };
  const body = { event: 'call_analyzed', call: { call_id: 'synthetic-unmatched', direction: 'inbound', from_number: '+12025550100', call_analysis: {} } };
  assert.equal((await handlePostCall(webhookReq(body), { env, fetch, now: () => now })).status, 200);
  assert.equal(row.lead_id, null); assert.equal(row.outcome, 'Unknown');
  const skipped = await handlePostCall(webhookReq({ event: 'call_ended' }), { env, fetch: () => { throw Error('must not write'); }, now: () => now });
  assert.equal(skipped.status, 200); assert.equal((await skipped.json()).skipped, 'call_ended');
});
test('PostgREST empty 201 responses are successful, not JSON parsing failures', async () => {
  const result = await supabase({ env, fetch: async () => new Response(null, { status: 201 }) }, 'activity_log', { method: 'POST' });
  assert.equal(result, null);
});
