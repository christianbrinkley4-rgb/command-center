import "jsr:@supabase/functions-js/edge-runtime.d.ts";

// Checks the live status of a batch of Retell calls.
//
// Body: { call_ids: string[] }
// Returns: {
//   active: [{ call_id, status }],
//   completed: [{ call_id, status }],
// }
//
// A call with no retrievable status (404, network error) is treated as
// completed — we never want a ghost call to block the autonomous loop forever.

const CORS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "authorization, x-client-info, apikey, content-type, x-voice-secret",
  "Access-Control-Allow-Methods": "POST, OPTIONS",
};

// Retell call statuses that mean the call is finished.
const TERMINAL = new Set([
  "ended",
  "error",
  "call_ended",
  "completed",
  "failed",
  "no_answer",
  "voicemail",
  "busy",
  "disconnected",
]);

Deno.serve(async (req: Request) => {
  if (req.method === "OPTIONS") return new Response("ok", { headers: CORS });
  const json = (body: unknown, status = 200) =>
    new Response(JSON.stringify(body), { status, headers: { ...CORS, "Content-Type": "application/json" } });

  const RETELL_API_KEY = Deno.env.get("RETELL_API_KEY") || "";
  const VOICE_API_SECRET = Deno.env.get("VOICE_API_SECRET") || "";
  if (!RETELL_API_KEY) {
    return json({ error: "server_misconfigured", detail: "RETELL_API_KEY not set" }, 500);
  }
  if (VOICE_API_SECRET && req.headers.get("x-voice-secret") !== VOICE_API_SECRET) {
    return json({ error: "unauthorized" }, 401);
  }

  let body: { call_ids?: string[] };
  try {
    body = await req.json();
  } catch {
    return json({ error: "bad_request" }, 400);
  }

  const callIds = (body.call_ids || []).filter(Boolean);
  if (callIds.length === 0) {
    return json({ active: [], completed: [] });
  }

  type StatusResult = { call_id: string; status: string; terminal: boolean };

  const results: StatusResult[] = await Promise.all(
    callIds.map(async (call_id): Promise<StatusResult> => {
      try {
        const res = await fetch(`https://api.retellai.com/v2/get-call/${call_id}`, {
          headers: { Authorization: `Bearer ${RETELL_API_KEY}` },
        });
        if (!res.ok) {
          // Can't see the call — treat as done so the loop never stalls.
          return { call_id, status: "unknown", terminal: true };
        }
        const data = await res.json();
        const status: string =
          data.call_status || data.status || data.call?.call_status || "unknown";
        const terminal = TERMINAL.has(String(status).toLowerCase());
        return { call_id, status, terminal };
      } catch {
        return { call_id, status: "unknown", terminal: true };
      }
    })
  );

  return json({
    active: results.filter((r) => !r.terminal).map((r) => ({ call_id: r.call_id, status: r.status })),
    completed: results.filter((r) => r.terminal).map((r) => ({ call_id: r.call_id, status: r.status })),
  });
});
