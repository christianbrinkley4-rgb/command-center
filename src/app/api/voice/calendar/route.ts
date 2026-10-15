// Next.js adapter for the shared server handler.
import { handleCalendar } from '../../../../../voice-api/calendar.mjs';
export const runtime = 'nodejs';
export async function POST(req: Request) { return handleCalendar(req); }
