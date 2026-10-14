import { NextRequest, NextResponse } from "next/server";
import { createClient } from "@supabase/supabase-js";

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
};

export async function POST(req: NextRequest) {
  try {
    const body = await req.json();
    const event = body.event;

    if (event !== "call_ended" && event !== "call_analyzed") {
      return NextResponse.json({ received: true, skipped: event });
    }

    const call = body.call || {};
    const leadId = call.metadata?.lead_id;

    if (!leadId) {
      console.error("Voice webhook: missing lead_id in metadata");
      return NextResponse.json({ error: "missing lead_id" }, { status: 400 });
    }

    const transcript = call.transcript || "";
    const summary = body.call_analysis?.call_summary || call.call_summary || "";
    const retellOutcome = body.call_analysis?.custom_analysis_data?.outcome || "no_answer";
    const disposition = DISPOSITION_MAP[retellOutcome] || "No Answer";
    const durationSec = Math.round((call.duration_ms || 0) / 1000);
    const sentiment = body.call_analysis?.user_sentiment || "unknown";

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
      notes: notes,
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
