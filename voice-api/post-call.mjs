import { ApiError, failure, hex, json, runtime, sameSecret, stableUuid, supabase, text, uuid } from './common.mjs';
const DISPOSITIONS = { booked: 'Appointment Set', interested_callback: 'Callback', not_interested: 'Not Interested', voicemail_left: 'Left Voicemail', no_answer: 'No Answer', wrong_number: 'Wrong Number', do_not_call: 'Do Not Call', disconnected: 'Disconnected' };
export async function verifySignature(raw, header, secret, now = Date.now()) {
  if (!secret || !header) return false;
  const match = /^v=(\d+),d=([0-9a-f]{64})$/i.exec(header);
  if (!match || Math.abs(now - Number(match[1])) > 300000) return false;
  const key = await crypto.subtle.importKey('raw', new TextEncoder().encode(secret), { name: 'HMAC', hash: 'SHA-256' }, false, ['sign']);
  return sameSecret(match[2].toLowerCase(), hex(await crypto.subtle.sign('HMAC', key, new TextEncoder().encode(raw + match[1]))));
}
async function resolveLead(ctx, call) {
  const supplied = call.metadata?.lead_id || call.retell_llm_dynamic_variables?.lead_id;
  if (supplied) {
    if (!uuid(supplied)) throw new ApiError(400, 'invalid lead_id');
    const rows = await supabase(ctx, `leads?select=id&id=eq.${encodeURIComponent(supplied)}&limit=1`);
    return rows?.[0]?.id || null;
  }
  const phone = text(call.direction === 'inbound' ? call.from_number : call.to_number).replace(/\D/g, '');
  const local = phone.length === 11 && phone.startsWith('1') ? phone.slice(1) : phone;
  if (local.length !== 10) return null;
  const variants = [local, `1${local}`, `+1${local}`, `(${local.slice(0,3)}) ${local.slice(3,6)}-${local.slice(6)}`, `${local.slice(0,3)}-${local.slice(3,6)}-${local.slice(6)}`];
  const alternatives = variants.flatMap(v => [`phone.eq."${v}"`, `phone2.eq."${v}"`]);
  const rows = await supabase(ctx, `leads?${new URLSearchParams({ select: 'id', or: `(${alternatives.join(',')})`, limit: '2' })}`);
  return rows?.length === 1 ? rows[0].id : null;
}
export async function handlePostCall(req, options = {}) {
  const ctx = runtime(options);
  if (req.method !== 'POST') return json({ error: 'POST required' }, 405);
  const secret = ctx.env.RETELL_WEBHOOK_SECRET || ctx.env.RETELL_API_KEY;
  if (!secret) return json({ error: 'webhook signing key is not configured' }, 503);
  try {
    const raw = await req.text();
    if (!(await verifySignature(raw, req.headers.get('x-retell-signature'), secret, ctx.now()))) return json({ error: 'bad signature' }, 401);
    let body;
    try { body = JSON.parse(raw); } catch { throw new ApiError(400, 'invalid JSON body'); }
    if (body?.event !== 'call_analyzed') return json({ received: true, skipped: body?.event || 'unknown' });
    const call = body.call;
    if (!call || !text(call.call_id)) throw new ApiError(400, 'call_id is required');
    const analysis = call.call_analysis || {};
    const custom = analysis.custom_analysis_data || {};
    const outcome = custom.disposition || custom.outcome || (/^dial_(no_answer|busy|failed)$/.test(call.disconnection_reason || '') ? 'no_answer' : 'unknown');
    const disposition = DISPOSITIONS[outcome] || 'Unknown';
    const leadId = await resolveLead(ctx, call);
    const duration = Number.isFinite(call.duration_ms) ? Math.max(0, Math.round(call.duration_ms / 1000)) : 0;
    const notes = [`Retell call: ${call.call_id}`, `From: ${text(call.from_number)}`, `To: ${text(call.to_number)}`, `Direction: ${text(call.direction)}`, `Duration: ${duration}s`, `Outcome: ${disposition}`, `Sentiment: ${text(analysis.user_sentiment) || 'Unknown'}`, `Summary: ${text(analysis.call_summary)}`, leadId ? '' : 'No unique CRM lead matched. Review this call.', text(call.transcript) ? `Transcript:\n${call.transcript}` : ''].filter(Boolean).join('\n');
    // Atomic primary-key upsert deduplicates concurrent retries. Unmatched
    // inbound calls are retained using the nullable production lead_id column.
    await supabase(ctx, 'activity_log?on_conflict=id', { method: 'POST', headers: { Prefer: 'resolution=ignore-duplicates,return=minimal' }, body: JSON.stringify({ id: await stableUuid(`retell-call:${call.call_id}`), lead_id: leadId, activity_type: 'AI Call', outcome: disposition, notes, logged_by: 'Voice Agent' }) });
    if (leadId) {
      const update = { updated_at: new Date(ctx.now()).toISOString() };
      if (outcome === 'do_not_call') Object.assign(update, { do_not_call: true, dnc_reason: 'requested' });
      await supabase(ctx, `leads?id=eq.${encodeURIComponent(leadId)}`, { method: 'PATCH', headers: { Prefer: 'return=minimal' }, body: JSON.stringify(update) });
    }
    return json({ received: true, call_id: call.call_id, lead_id: leadId, disposition });
  } catch (error) { return failure(error); }
}
