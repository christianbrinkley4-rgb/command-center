import { ApiError, digest, failure, json, requestJson, runtime, sameSecret, stableUuid, supabase, text, uuid } from './common.mjs';
export const TIMEZONE = 'America/New_York';
const SLOT_MINUTES = 30;
export function validDate(date) {
  return typeof date === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(date) && !Number.isNaN(Date.parse(`${date}T12:00:00Z`)) && new Date(`${date}T12:00:00Z`).toISOString().slice(0, 10) === date;
}
export function zonedDateTime(date, time) {
  // Offered working hours fall after the Eastern DST transition hour.
  const parts = new Intl.DateTimeFormat('en-US', { timeZone: TIMEZONE, timeZoneName: 'shortOffset' }).formatToParts(new Date(`${date}T12:00:00Z`));
  const match = parts.find(p => p.type === 'timeZoneName').value.match(/GMT([+-])(\d+)(?::(\d+))?/);
  if (!match) throw new ApiError(502, 'calendar timezone unavailable');
  return `${date}T${time}:00${match[1]}${match[2].padStart(2, '0')}:${(match[3] || '00').padStart(2, '0')}`;
}
const hhmm = minutes => `${String(Math.floor(minutes / 60)).padStart(2, '0')}:${String(minutes % 60).padStart(2, '0')}`;
async function accessToken(ctx) {
  const { GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, GOOGLE_REFRESH_TOKEN } = ctx.env;
  if (!GOOGLE_CLIENT_ID || !GOOGLE_CLIENT_SECRET || !GOOGLE_REFRESH_TOKEN) throw new ApiError(503, 'Google Calendar is not configured');
  const response = await ctx.fetch('https://oauth2.googleapis.com/token', { method: 'POST', headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
    body: new URLSearchParams({ client_id: GOOGLE_CLIENT_ID, client_secret: GOOGLE_CLIENT_SECRET, refresh_token: GOOGLE_REFRESH_TOKEN, grant_type: 'refresh_token' }), signal: AbortSignal.timeout(10000) });
  if (!response.ok) throw new ApiError(502, 'Google authorization failed');
  const data = await response.json();
  if (!data.access_token) throw new ApiError(502, 'Google authorization failed');
  return data.access_token;
}
async function google(ctx, token, path, init = {}) {
  return ctx.fetch(`https://www.googleapis.com/calendar/v3${path}`, { ...init, headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' }, signal: AbortSignal.timeout(10000) });
}
async function availability(ctx, token, date) {
  const calendarId = ctx.env.GOOGLE_CALENDAR_ID || 'primary';
  const response = await google(ctx, token, '/freeBusy', { method: 'POST', body: JSON.stringify({ timeMin: zonedDateTime(date, '09:00'), timeMax: zonedDateTime(date, '17:00'), timeZone: TIMEZONE, items: [{ id: calendarId }] }) });
  if (!response.ok) throw new ApiError(502, 'calendar availability request failed');
  const calendar = (await response.json()).calendars?.[calendarId];
  if (!calendar || calendar.errors?.length || !Array.isArray(calendar.busy)) throw new ApiError(502, 'calendar availability unavailable');
  const busy = calendar.busy.map(item => [Date.parse(item.start), Date.parse(item.end)]);
  if (busy.some(([start, end]) => !Number.isFinite(start) || !Number.isFinite(end) || end < start)) throw new ApiError(502, 'calendar availability unavailable');
  const slots = [];
  for (let minute = 9 * 60; minute + SLOT_MINUTES <= 17 * 60; minute += SLOT_MINUTES) {
    const start = Date.parse(zonedDateTime(date, hhmm(minute)));
    if (start > ctx.now() && !busy.some(([bs, be]) => start < be && start + SLOT_MINUTES * 60000 > bs)) slots.push(hhmm(minute));
  }
  return slots;
}
async function logAppointment(ctx, body, event, start) {
  if (!body.lead_id) return;
  await supabase(ctx, 'activity_log?on_conflict=id', { method: 'POST', headers: { Prefer: 'resolution=ignore-duplicates,return=minimal' }, body: JSON.stringify({ id: await stableUuid(`retell-booking:${event.id}`), lead_id: body.lead_id, activity_type: 'Appointment Booked', outcome: 'Appointment Set', notes: `AI voice agent booked ${body.date} at ${body.time} Eastern with ${body.lead_name} (${body.lead_phone})`, logged_by: 'Voice Agent' }) });
  await supabase(ctx, `leads?id=eq.${encodeURIComponent(body.lead_id)}`, { method: 'PATCH', headers: { Prefer: 'return=minimal' }, body: JSON.stringify({ appointment_datetime: start, updated_at: new Date(ctx.now()).toISOString() }) });
}
export async function handleCalendar(req, options = {}) {
  const ctx = runtime(options);
  if (req.method !== 'POST') return json({ error: 'POST required' }, 405);
  if (!ctx.env.VOICE_API_SECRET) return json({ error: 'voice API is not configured' }, 503);
  if (!sameSecret(req.headers.get('x-voice-secret'), ctx.env.VOICE_API_SECRET)) return json({ error: 'unauthorized' }, 401);
  try {
    const envelope = await requestJson(req);
    // Supports Retell's default {name,args,call} envelope and args-only mode.
    const body = envelope.args && typeof envelope.args === 'object' ? envelope.args : envelope;
    const action = body.action || envelope.name;
    if (!['check_availability', 'book_appointment'].includes(action)) throw new ApiError(400, 'unknown action');
    if (!validDate(body.date)) throw new ApiError(400, 'date required as a real YYYY-MM-DD date');
    if (body.lead_id && !uuid(body.lead_id)) throw new ApiError(400, 'lead_id must be a UUID or empty');
    if (action === 'book_appointment') {
      if (!/^(09|1[0-6]):(00|30)$/.test(text(body.time))) throw new ApiError(400, 'time must be a 30-minute slot from 09:00 through 16:30 Eastern');
      if (!text(body.lead_name) || !text(body.lead_phone)) throw new ApiError(400, 'lead_name and lead_phone are required');
      if (body.lead_id && !ctx.env.SUPABASE_SERVICE_ROLE_KEY) throw new ApiError(503, 'CRM is not configured');
    }
    const token = await accessToken(ctx);
    if (action === 'check_availability') return json({ date: body.date, timezone: TIMEZONE, duration_minutes: SLOT_MINUTES, slots: await availability(ctx, token, body.date) });
    const calendarId = ctx.env.GOOGLE_CALENDAR_ID || 'primary';
    const path = `/calendars/${encodeURIComponent(calendarId)}/events`;
    // Stable slot ID prevents duplicate events on retries and competing AI bookings.
    const id = `retell${await digest(`${calendarId}:${body.date}:${body.time}`)}`;
    const fingerprint = await digest(`${body.lead_id || ''}:${body.lead_name}:${body.lead_phone}`);
    const existing = await google(ctx, token, `${path}/${id}`);
    let event;
    if (existing.ok) {
      event = await existing.json();
      if (event.status === 'cancelled' || event.extendedProperties?.private?.voiceBooking !== fingerprint) throw new ApiError(409, 'slot is no longer available');
    } else {
      if (existing.status !== 404) throw new ApiError(502, 'calendar booking lookup failed');
      if (!(await availability(ctx, token, body.date)).includes(body.time)) throw new ApiError(409, 'slot is no longer available');
      const minutes = Number(body.time.slice(0, 2)) * 60 + Number(body.time.slice(3));
      const created = await google(ctx, token, path, { method: 'POST', body: JSON.stringify({ id, summary: `Medicare appointment - ${text(body.lead_name)}`,
        description: [`Phone: ${text(body.lead_phone)}`, text(body.notes) ? `Notes: ${text(body.notes)}` : '', body.lead_id ? `Lead ID: ${body.lead_id}` : '', 'Booked by Retell voice agent'].filter(Boolean).join('\n'),
        start: { dateTime: zonedDateTime(body.date, body.time), timeZone: TIMEZONE }, end: { dateTime: zonedDateTime(body.date, hhmm(minutes + SLOT_MINUTES)), timeZone: TIMEZONE }, extendedProperties: { private: { voiceBooking: fingerprint } } }) });
      if (created.status === 409) throw new ApiError(409, 'slot was just taken; check availability again');
      if (!created.ok) throw new ApiError(502, 'calendar booking failed');
      event = await created.json();
    }
    if (!event.id) throw new ApiError(502, 'calendar did not confirm the event');
    let crmLogged = !body.lead_id;
    try { await logAppointment(ctx, body, event, zonedDateTime(body.date, body.time)); crmLogged = true; } catch { /* The event exists. Do not create another one or misreport booking failure. */ }
    return json({ booked: true, event_id: event.id, event_link: event.htmlLink || '', date: body.date, time: body.time, timezone: TIMEZONE, duration_minutes: SLOT_MINUTES, crm_logged: crmLogged });
  } catch (error) { return failure(error); }
}
