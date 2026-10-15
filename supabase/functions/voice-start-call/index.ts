import "jsr:@supabase/functions-js/edge-runtime.d.ts";
import { createClient } from "jsr:@supabase/supabase-js@2";

// Starts one AI outbound call via Retell.
//
// Body: { lead_id: string }
// Returns: { call_id, lead_id, to_number }
//
// Blocks DNC-flagged leads (409) and leads with no callable number (422).
// Logs "AI Call Started" to activity_log so the book shows the attempt
// even before the post-call webhook fires.

const CORS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "authorization, x-client-info, apikey, content-type, x-voice-secret",
  "Access-Control-Allow-Methods": "POST, OPTIONS",
};

const AGENT_ID = "agent_3bd8649f9aee7d7b92b6d483a8";
const FROM_NUMBER = "+13369679358";

/** Split "John Smith" into first/last for the agent's dynamic variables. */
function splitName(full: string | null): { first_name: string; last_name: string } {
  if (!full) return { first_name: "there", last_name: "" };
  const parts = full.trim().split(/\s+/);
  return {
    first_name: parts[0] || "there",
    last_name: parts.slice(1).join(" ") || "",
  };
}

/** Normalize a US phone to E.164. Returns null if it can't be salvaged. */
function toE164(raw: string | null): string | null {
  if (!raw) return null;
  const digits = raw.replace(/\D/g, "");
  if (digits.length === 10) return `+1${digits}`;
  if (digits.length === 11 && digits.startsWith("1")) return `+${digits}`;
  return null;
}

/** "1961-03-15" -> "March". Null-safe. */
function turning65Month(birthday: string | null): string {
  if (!birthday) return "";
  const m = birthday.match(/^\d{4}-(\d{2})-\d{2}/);
  if (!m) return "";
  const months = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
  ];
  const idx = parseInt(m[1], 10) - 1;
  return months[idx] || "";
}

Deno.serve(async (req: Request) => {
  if (req.method === "OPTIONS") return new Response("ok", { headers: CORS });
  const json = (body: unknown, status = 200) =>
    new Response(JSON.stringify(body), { status, headers: { ...CORS, "Content-Type": "application/json" } });

  const SUPABASE_URL = Deno.env.get("SUPABASE_URL") || "";
  const SUPABASE_SERVICE_KEY = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY") || "";
  const RETELL_API_KEY = Deno.env.get("RETELL_API_KEY") || "";
  const VOICE_API_SECRET = Deno.env.get("VOICE_API_SECRET") || "";

  // Shared-secret gate, same as voice-calendar. Placing calls costs money,
  // so this stays closed unless the caller knows the secret.
  if (VOICE_API_SECRET && req.headers.get("x-voice-secret") !== VOICE_API_SECRET) {
    return json({ error: "unauthorized" }, 401);
  }

  if (!SUPABASE_URL || !SUPABASE_SERVICE_KEY) {
    return json({ error: "server_misconfigured", detail: "SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY not set" }, 500);
  }
  if (!RETELL_API_KEY) {
    return json({ error: "server_misconfigured", detail: "RETELL_API_KEY not set" }, 500);
  }

  let body: { lead_id?: string };
  try {
    body = await req.json();
  } catch {
    return json({ error: "bad_request" }, 400);
  }
  if (!body.lead_id) {
    return json({ error: "bad_request", detail: "lead_id required" }, 400);
  }

  const supabase = createClient(SUPABASE_URL, SUPABASE_SERVICE_KEY);
  const { data: lead, error: leadErr } = await supabase
    .from("leads")
    .select("id, name, phone, phone2, city, state, zip, address, birthday, source, oscr_lead_source, notes, raw_notes, do_not_call")
    .eq("id", body.lead_id)
    .single();

  if (leadErr || !lead) {
    return json({ error: "lead_not_found" }, 404);
  }
  if (lead.do_not_call) {
    return json({ error: "do_not_call", detail: "Lead is flagged do-not-call" }, 409);
  }

  const toNumber = toE164(lead.phone) || toE164(lead.phone2);
  if (!toNumber) {
    return json({ error: "no_callable_number" }, 422);
  }

  const { first_name, last_name } = splitName(lead.name);
  const notes = [lead.notes, lead.raw_notes].filter(Boolean).join("\n").slice(0, 1000);

  const retellBody = {
    from_number: FROM_NUMBER,
    to_number: toNumber,
    agent_id: AGENT_ID,
    retell_llm_dynamic_variables: {
      first_name,
      last_name,
      phone: toNumber,
      city: lead.city || "",
      state: lead.state || "",
      zip: lead.zip || "",
      address: lead.address || "",
      turning_65_month: turning65Month(lead.birthday),
      lead_notes: notes,
      lead_source: lead.oscr_lead_source || lead.source || "",
      lead_id: lead.id,
    },
  };

  let retellRes: Response;
  try {
    retellRes = await fetch("https://api.retellai.com/v2/create-phone-call", {
      method: "POST",
      headers: {
        Authorization: `Bearer ${RETELL_API_KEY}`,
        "Content-Type": "application/json",
      },
      body: JSON.stringify(retellBody),
    });
  } catch (e) {
    console.error("Retell create-phone-call request failed:", e);
    return json({ error: "retell_failed", detail: "network error" }, 502);
  }

  if (!retellRes.ok) {
    const text = await retellRes.text();
    console.error(`Retell create-phone-call failed (${retellRes.status}): ${text.slice(0, 500)}`);
    return json({ error: "retell_failed", detail: text.slice(0, 200) }, 502);
  }

  const retellData = await retellRes.json();
  const callId = retellData.call_id || retellData.callId || null;

  // Log the outbound attempt so the book shows it even before the webhook fires.
  try {
    await supabase.from("activity_log").insert({
      lead_id: lead.id,
      activity_type: "Call",
      outcome: "AI Call Started",
      notes: `AI agent dialing ${toNumber} (Retell call ${callId || "pending"})`,
    });
  } catch {
    // Non-fatal: the call is already placed.
  }

  return json({ call_id: callId, lead_id: lead.id, to_number: toNumber });
});
