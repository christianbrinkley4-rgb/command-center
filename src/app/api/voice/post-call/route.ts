import { NextRequest, NextResponse } from "next/server";
import { createClient } from "@supabase/supabase-js";
import { createHmac, timingSafeEqual } from "crypto";

const SUPABASE_URL = "https://lyvhrxiukmlvrznkrtkv.supabase.co";
const SUPABASE_SERVICE_KEY = process.env.SUPABASE_SERVICE_ROLE_KEY || "";

function getSupabase() {
  return createClient(SUPABASE_URL, SUPABASE_SERVICE_KEY);
}

const DISPOSITION_MAP: Record<string, string> = {
  booked: "Appointment Set",
  interested_callback: "Callback",
  not_interested: "Not Interested",
  voicemail_left: "Left Voicemail",
  no_answer: "No Answer",
  wrong_number: "Wrong Number",
  do_not_call: "Do Not Call",
  disconnected: "Disconnected",
};

/**
 * Verifies the Retell webhook signature.
 * Retell signs the raw request body with HMAC-SHA256 and sends it in the
 * `x-retell-signature` header, either as `sha256=<hex>` or bare `<hex>`.
 * Signing key: RETELL_WEBHOOK_SECRET if set, otherwise the Retell API key
 * (RETELL_API_KEY), which is what Retell documents as the signing key.
 * Fails closed: if no signing key is configured, every request is rejected.
 */
function verifySignature(rawBody: string, req: NextRequest): boolean {
  const secret = process.env.RETELL_WEBHOOK_SECRET || process.env.RETELL_API_KEY;
  if (!secret) {
    console.error("Voice webhook: no RETELL_WEBHOOK_SECRET or RETELL_API_KEY set");
    return false;
  }
  const header = req.headers.get("x-retell-signature");
  if (!header) return false;
  const provided = header.startsWith("sha256=") ? header.slice(7) : header;
  const expected = createHmac("sha256", secret).update(rawBody, "utf8").digest("hex");
  const a = Buffer.from(provided, "utf8");
  const b = Buffer.from(expected, "utf8");
  if (a.length !== b.length) return false;
  return timingSafeEqual(a, b);
}

export async function POST(req: NextRequest) {
  let body: any;
  try {
    // Read the raw body first: signature verification needs the exact bytes.
    const rawBody = await req.text();
    if (!verifySignature(rawBody, req)) {
      return NextResponse.json({ error: "bad signature" }, { status: 401 });
    }
    body = JSON.parse(rawBody);
  } catch (err) {
    console.error("Voice webhook: invalid request body", err);
    return NextResponse.json({ error: "invalid body" }, { status: 400 });
  }

  const event = body.event;

  // IDEMPOTENCY: Retell fires BOTH call_ended and call_analyzed per call.
  // call_analyzed carries the richer data (summary, sentiment, custom analysis),
  // so we process it and skip call_ended. One call = exactly one CRM record.
  // The Retell dashboard must have call_analyzed selected in webhook events.
  if (event !== "call_analyzed") {
    return NextResponse.json({ received: true, skipped: event });
  }

  try {
    const call = body.call || {};
    const leadId = call.metadata?.lead_id;

    if (!leadId) {
      console.error("Voice webhook: missing lead_id in metadata");
      return NextResponse.json({ error: "missing lead_id" }, { status: 400 });
    }

    const callId = call.call_id || "";
    const transcript = call.transcript || "";
    const analysis = body.call_analysis || {};
    const custom = analysis.custom_analysis_data || {};
    const summary = analysis.call_summary || call.call_summary || "";
    const retellOutcome = custom.outcome || "no_answer";
    const disposition = DISPOSITION_MAP[retellOutcome] || "No Answer";
    const durationSec = Math.round((call.duration_ms || 0) / 1000);
    const sentiment = analysis.user_sentiment || "unknown";

    const notes = [
      `AI voice call (${Math.floor(durationSec / 60)}m ${durationSec % 60}s)`,
      `Outcome: ${disposition}`,
      `Sentiment: ${sentiment}`,
      summary ? `Summary: ${summary}` : "",
    ]
      .filter(Boolean)
      .join("\n");

    const supabase = getSupabase();

    const { error: activityError } = await supabase.from("activity_log").insert({
      lead_id: leadId,
      activity_type: "AI Call",
      outcome: disposition,
      notes: callId ? `${notes}\nRetell call: ${callId}` : notes,
      logged_by: "Voice Agent",
    });

    if (activityError) throw activityError;

    if (transcript) {
      await supabase.from("activity_log").insert({
        lead_id: leadId,
        activity_type: "AI Call Transcript",
        outcome: null,
        notes: transcript.substring(0, 5000),
        logged_by: "Voice Agent",
      });
    }

    await supabase
      .from("leads")
      .update({ last_touch: new Date().toISOString() })
      .eq("id", leadId);

    return NextResponse.json({ received: true, lead_id: leadId, disposition });
  } catch (err) {
    console.error("Voice webhook error:", err);
    return NextResponse.json({ error: "internal error" }, { status: 500 });
  }
}
