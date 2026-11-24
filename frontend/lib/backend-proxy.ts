import { createHash, timingSafeEqual } from "node:crypto";

export const PIN_HEADER = "X-App-Pin";

export function hasValidPin(request: Request): boolean {
  const supplied = request.headers.get(PIN_HEADER);
  const expected = process.env.APP_ACCESS_PIN;
  if (!supplied || !expected) return false;
  const suppliedDigest = createHash("sha256").update(supplied).digest();
  const expectedDigest = createHash("sha256").update(expected).digest();
  return timingSafeEqual(suppliedDigest, expectedDigest);
}

export function privateResponse(message: string, status: number): Response {
  return Response.json({ message }, { status, headers: { "Cache-Control": "no-store" } });
}

export function fetchBackend(request: Request, path: string, init?: RequestInit) {
  const base = process.env.BACKEND_INTERNAL_URL || "http://127.0.0.1:8000";
  return fetch(new URL(path, base), {
    ...init,
    signal: request.signal,
    cache: "no-store",
    redirect: "error",
  });
}

export function isJson(response: Response): boolean {
  return response.headers.get("Content-Type")?.split(";")[0].trim() === "application/json";
}

/**
 * Forward a JSON GET to the backend after the caller checked the PIN and validated input.
 * Upstream error bodies never reach the browser: any non-JSON or failed reply becomes
 * `failure`, and a network error becomes `unreachable`. Listed headers pass through both ways.
 */
export async function forwardJson(request: Request, path: string, messages: { failure: string; unreachable: string },
                                  passHeaders: string[] = []): Promise<Response> {
  let upstream: Response;
  try {
    upstream = await fetchBackend(request, path);
  } catch {
    return privateResponse(messages.unreachable, 502);
  }
  const ok = upstream.ok && upstream.body && isJson(upstream);
  const response = ok
    ? new Response(upstream.body, { headers: { "Content-Type": "application/json", "Cache-Control": "no-store" } })
    : privateResponse(messages.failure, 502);
  if (!ok) await upstream.body?.cancel();
  for (const name of passHeaders) {
    const value = upstream.headers.get(name);
    if (value) response.headers.set(name, value);
  }
  return response;
}
