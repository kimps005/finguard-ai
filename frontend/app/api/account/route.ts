import { fetchBackend, hasValidPin, privateResponse } from "../../../lib/backend-proxy.ts";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

export async function GET(request: Request): Promise<Response> {
  if (!hasValidPin(request)) return privateResponse("Unauthorized", 401);

  try {
    const upstream = await fetchBackend(request, "/api/v1/account");
    if (upstream.ok && upstream.body &&
        upstream.headers.get("Content-Type")?.split(";")[0].trim() === "application/json") {
      return new Response(upstream.body, {
        headers: { "Content-Type": "application/json", "Cache-Control": "no-store" },
      });
    }
    await upstream.body?.cancel();
  } catch {
    return privateResponse("계좌 서버에 연결할 수 없습니다.", 502);
  }
  return privateResponse("계좌 정보를 불러올 수 없습니다.", 502);
}
