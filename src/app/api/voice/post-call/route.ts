// Next.js adapter for the shared server handler.
import { handlePostCall } from '../../../../../voice-api/post-call.mjs';
export const runtime = 'nodejs';
export async function POST(req: Request) { return handlePostCall(req); }
