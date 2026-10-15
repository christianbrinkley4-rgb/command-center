import "jsr:@supabase/functions-js/edge-runtime.d.ts";

// Starts up to 5 concurrent AI calls. Thin wrapper over the
// voice-start-call edge function: fires one start-call request per lead_id
// in parallel and collects results.
//
// Body: { lead_ids: string[] }
// Returns: { calls: [{ lead_id, call_id, to_number }], errors: [{ lead_id, error }], started: n }

const CORS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "authorization, x-client-info, apikey, content-type, x-voice-secret",
  "Access-Control-Allow-Methods": "POST, OPTIONS",
};

const MAX_BATCH = 5;
const START_CALL_URL = "https://lyvhrxiukmlvrznkrtkv.supabase.co/functions/v1/voice-start-call";

Deno.serve(async (req: Request) => {
  if (req.method === "OPTIONS") return new Response("ok", { headers: CORS });
  const json = (body: unknown, status = 200) =>
    new Response(JSON.stringify(body), { status, headers: { ...CORS, "Content-Type": "application/json" } });

  let body: { lead_ids?: string[] };
  try {
    body = await req.json();
  } catch {
    return json({ error: "bad_request" }, 400);
  }

  const leadIds = (body.lead_ids || []).filter(Boolean).slice(0, MAX_BATCH);
  if (leadIds.length === 0) {
    return json({ error: "bad_request", detail: "lead_ids required (1-5)" }, 400);
  }

  // Forward the voice secret so start-call stays behind the same gate.
  const forwardHeaders: Record<string, string> = { "Content-Type": "application/json" };
  const voiceSecret = req.headers.get("x-voice-secret");
  if (voiceSecret) forwardHeaders["x-voice-secret"] = voiceSecret;
  const anonKey = Deno.env.get("SUPABASE_ANON_KEY") || "";
  if (anonKey) forwardHeaders["apikey"] = anonKey;

  type OkResult = { lead_id: string; ok: true; call_id: string | null; to_number: string };
  type ErrResult = { lead_id: string; ok: false; error: string; detail?: string };
  type Result = OkResult | ErrResult;

  const results: Result[] = await Promise.all(
    leadIds.map(async (lead_id): Promise<Result> => {
      try {
        const res = await fetch(START_CALL_URL, {
          method: "POST",
          headers: forwardHeaders,
          body: JSON.stringify({ lead_id }),
        });
        const data = await res.json();
        if (!res.ok) {
          return { lead_id, ok: false, error: data.error || "start_failed", detail: data.detail };
        }
        return { lead_id, ok: true, call_id: data.call_id ?? null, to_number: data.to_number };
      } catch (e) {
        return { lead_id, ok: false, error: "request_failed", detail: e instanceof Error ? e.message : "unknown" };
      }
    })
  );

  const calls = results.filter((r): r is OkResult => r.ok);
  const errors = results.filter((r): r is ErrResult => !r.ok);

  return json({ calls, errors, started: calls.length });
});
