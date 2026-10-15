import { NextRequest, NextResponse } from "next/server";
import { createClient } from "@supabase/supabase-js";

const SUPABASE_URL = "https://lyvhrxiukmlvrznkrtkv.supabase.co";
const SUPABASE_SERVICE_KEY = process.env.SUPABASE_SERVICE_ROLE_KEY || "";

const GOOGLE_CLIENT_ID = process.env.GOOGLE_CLIENT_ID || "";
const GOOGLE_CLIENT_SECRET = process.env.GOOGLE_CLIENT_SECRET || "";
const GOOGLE_REFRESH_TOKEN = process.env.GOOGLE_REFRESH_TOKEN || "";
const GOOGLE_CALENDAR_ID = process.env.GOOGLE_CALENDAR_ID || "primary";

const TIMEZONE = "America/New_York";
const SLOT_MINUTES = 30;
const DAY_START_HOUR = 9;
const DAY_END_HOUR = 17;

function getSupabase() {
  return createClient(SUPABASE_URL, SUPABASE_SERVICE_KEY);
}

/** Exchange the stored refresh token for a fresh access token. */
async function getAccessToken(): Promise<string> {
  if (!GOOGLE_CLIENT_ID || !GOOGLE_CLIENT_SECRET || !GOOGLE_REFRESH_TOKEN) {
    throw new Error(
      "Google Calendar not configured: set GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, GOOGLE_REFRESH_TOKEN"
    );
  }
  const res = await fetch("https://oauth2.googleapis.com/token", {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({
      client_id: GOOGLE_CLIENT_ID,
      client_secret: GOOGLE_CLIENT_SECRET,
      refresh_token: GOOGLE_REFRESH_TOKEN,
      grant_type: "refresh_token",
    }),
  });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`Google token refresh failed (${res.status}): ${text.slice(0, 200)}`);
  }
  const data = await res.json();
  return data.access_token as string;
}

async function gcalFetch(path: string, accessToken: string, init?: RequestInit) {
  const res = await fetch(`https://www.googleapis.com/calendar/v3${path}`, {
    ...init,
    headers: {
      Authorization: `Bearer ${accessToken}`,
      "Content-Type": "application/json",
      ...(init?.headers || {}),
    },
  });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`Google Calendar API ${path} failed (${res.status}): ${text.slice(0, 300)}`);
  }
  return res.json();
}

function toMinutes(hhmm: string): number {
  const [h, m] = hhmm.split(":").map(Number);
  return h * 60 + m;
}

function toHHMM(minutes: number): string {
  const h = Math.floor(minutes / 60);
  const m = minutes % 60;
  return `${String(h).padStart(2, "0")}:${String(m).padStart(2, "0")}`;
}

/** Shared secret for Retell custom function calls. Rejects unauthenticated callers. */
function checkAuth(req: NextRequest): boolean {
  const secret = process.env.VOICE_API_SECRET;
  if (!secret) return true;
  const provided =
    req.headers.get("x-voice-secret") ||
    req.headers.get("authorization")?.replace(/^Bearer\s+/i, "");
  return provided === secret;
}

/** Parse an RFC3339 datetime and return minutes since midnight in its own offset. */
function zonedMinutes(iso: string): number | null {
  const m = iso.match(/T(\d{2}):(\d{2})(?::\d{2})?(Z|[+-]\d{2}:?\d{2})?/);
  if (!m) return null;
  return parseInt(m[1], 10) * 60 + parseInt(m[2], 10);
}

/** Build a Date in the target timezone for a given date + HH:MM. */
function zonedDateTime(dateStr: string, hhmm: string): string {
  return `${dateStr}T${hhmm}:00`;
}

/** EST/EDT offset for America/New_York on a given date. */
function nyOffset(dateStr: string): string {
  const d = new Date(`${dateStr}T12:00:00Z`);
  const jan = new Date(d.getFullYear(), 0, 1);
  const jul = new Date(d.getFullYear(), 6, 1);
  const stdOffset = Math.max(jan.getTimezoneOffset(), jul.getTimezoneOffset());
  void stdOffset;
  const fmt = new Intl.DateTimeFormat("en-US", {
    timeZone: TIMEZONE,
    timeZoneName: "shortOffset",
  });
  const parts = fmt.formatToParts(d);
  const tzName = parts.find((p) => p.type === "timeZoneName")?.value || "";
  const m = tzName.match(/GMT([+-])(\d{1,2})(?::(\d{2}))?/);
  if (!m) return "-05:00";
  const sign = m[1];
  const hh = m[2].padStart(2, "0");
  const mm = (m[3] || "00").padStart(2, "0");
  return `${sign}${hh}:${mm}`;
}

async function checkAvailability(date: string): Promise<string[]> {
  const accessToken = await getAccessToken();
  const offset = nyOffset(date);
  const timeMin = `${date}T00:00:00${offset}`;
  const timeMax = `${date}T23:59:59${offset}`;

  const data = await gcalFetch(
    `/calendars/${encodeURIComponent(GOOGLE_CALENDAR_ID)}/events?` +
      new URLSearchParams({
        timeMin,
        timeMax,
        singleEvents: "true",
        orderBy: "startTime",
      }),
    accessToken
  );

  const busy: Array<[number, number]> = [];
  for (const ev of data.items || []) {
    if (ev.status === "cancelled") continue;
    if (ev.transparency === "transparent") continue;
    const start = ev.start?.dateTime || ev.start?.date;
    const end = ev.end?.dateTime || ev.end?.date;
    if (!start || !end) continue;
    if (ev.start?.date && !ev.start?.dateTime) {
      busy.push([0, 24 * 60]);
      continue;
    }
    const sMin = zonedMinutes(start);
    const eMin = zonedMinutes(end);
    if (sMin === null || eMin === null) continue;
    busy.push([sMin, eMin]);
  }

  const slots: string[] = [];
  for (
    let m = DAY_START_HOUR * 60;
    m + SLOT_MINUTES <= DAY_END_HOUR * 60;
    m += SLOT_MINUTES
  ) {
    const slotEnd = m + SLOT_MINUTES;
    const overlaps = busy.some(([bs, be]) => m < be && slotEnd > bs);
    if (!overlaps) slots.push(toHHMM(m));
  }
  return slots;
}

async function bookAppointment(
  date: string,
  time: string,
  leadName: string,
  leadPhone: string,
  notes: string,
  leadId?: string
): Promise<{ event_id: string; event_link: string }> {
  const startMin = toMinutes(time);
  const endMin = startMin + SLOT_MINUTES;

  const accessToken = await getAccessToken();

  const event = {
    summary: `Medicare appointment — ${leadName}`,
    description: [
      `Phone: ${leadPhone}`,
      notes ? `Notes: ${notes}` : "",
      leadId ? `Lead ID: ${leadId}` : "",
      "Booked by Retell voice agent",
    ]
      .filter(Boolean)
      .join("\n"),
    start: { dateTime: zonedDateTime(date, time), timeZone: TIMEZONE },
    end: { dateTime: zonedDateTime(date, toHHMM(endMin)), timeZone: TIMEZONE },
  };

  const created = await gcalFetch(
    `/calendars/${encodeURIComponent(GOOGLE_CALENDAR_ID)}/events`,
    accessToken,
    { method: "POST", body: JSON.stringify(event) }
  );

  if (leadId && SUPABASE_SERVICE_KEY) {
    try {
      const supabase = getSupabase();
      await supabase.from("activity_log").insert({
        lead_id: leadId,
        activity_type: "Appointment Booked",
        outcome: "Appointment Set",
        notes: `AI voice agent booked ${date} at ${time} with ${leadName} (${leadPhone})`,
        logged_by: "Voice Agent",
      });
      await supabase
        .from("leads")
        .update({
          appointment_datetime: zonedDateTime(date, time),
          last_touch: new Date().toISOString(),
        })
        .eq("id", leadId);
    } catch (err) {
      console.error("Calendar webhook: Supabase log failed", err);
    }
  }

  return { event_id: created.id, event_link: created.htmlLink || "" };
}

export async function POST(req: NextRequest) {
  if (!checkAuth(req)) {
    return NextResponse.json({ error: "unauthorized" }, { status: 401 });
  }

  let body: any;
  try {
    body = await req.json();
  } catch {
    return NextResponse.json({ error: "invalid JSON body" }, { status: 400 });
  }

  const { action } = body;

  try {
    if (action === "check_availability") {
      const { date } = body;
      if (!date || !/^\d{4}-\d{2}-\d{2}$/.test(date)) {
        return NextResponse.json(
          { error: "date required as YYYY-MM-DD" },
          { status: 400 }
        );
      }
      const slots = await checkAvailability(date);
      return NextResponse.json({ date, slots });
    }

    if (action === "book_appointment") {
      const { date, time, lead_name, lead_phone, notes, lead_id } = body;
      if (!date || !time || !lead_name) {
        return NextResponse.json(
          { error: "date, time, and lead_name are required" },
          { status: 400 }
        );
      }
      if (!/^\d{4}-\d{2}-\d{2}$/.test(date) || !/^\d{2}:\d{2}$/.test(time)) {
        return NextResponse.json(
          { error: "date must be YYYY-MM-DD, time must be HH:MM" },
          { status: 400 }
        );
      }
      const result = await bookAppointment(
        date,
        time,
        lead_name,
        lead_phone || "",
        notes || "",
        lead_id
      );
      return NextResponse.json(result);
    }

    return NextResponse.json(
      { error: 'action must be "check_availability" or "book_appointment"' },
      { status: 400 }
    );
  } catch (err: any) {
    console.error("Calendar API error:", err?.message || err);
    return NextResponse.json(
      { error: err?.message || "calendar error" },
      { status: 502 }
    );
  }
}
