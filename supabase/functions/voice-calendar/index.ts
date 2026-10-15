import { handleCalendar } from '../../../voice-api/calendar.mjs';
const names = ['SUPABASE_URL', 'SUPABASE_SERVICE_ROLE_KEY', 'GOOGLE_CLIENT_ID', 'GOOGLE_CLIENT_SECRET', 'GOOGLE_REFRESH_TOKEN', 'GOOGLE_CALENDAR_ID', 'VOICE_API_SECRET', 'RETELL_WEBHOOK_SECRET', 'RETELL_API_KEY'];
Deno.serve((req: Request) => handleCalendar(req, { env: Object.fromEntries(names.map(name => [name, Deno.env.get(name)])) }));
