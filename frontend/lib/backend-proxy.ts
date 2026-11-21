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
  const base = process.env.BACKEND_INTERNAL_URL;
  if (!base) throw new Error("Missing BACKEND_INTERNAL_URL");
  return fetch(new URL(path, base), {
    ...init,
    signal: request.signal,
    cache: "no-store",
    redirect: "error",
  });
}
