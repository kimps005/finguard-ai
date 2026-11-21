import { fetchBackend, hasValidPin, privateResponse } from "../../../lib/backend-proxy.ts";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

export async function POST(request: Request): Promise<Response> {
  if (!hasValidPin(request)) return privateResponse("Unauthorized", 401);

  try {
    const upstream = await fetchBackend(request, "/api/v1/chat/stream", {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
      body: await request.text(),
    });
    if (upstream.ok && upstream.body &&
        upstream.headers.get("Content-Type")?.split(";")[0].trim() === "text/event-stream") {
      return new Response(upstream.body, {
        headers: {
          "Content-Type": "text/event-stream",
          "Cache-Control": "no-cache, no-transform",
          "X-Accel-Buffering": "no",
        },
      });
    }
    await upstream.body?.cancel();
    if (upstream.status === 422) return privateResponse("질문 형식을 확인해 주세요.", 422);
  } catch {
    return privateResponse("분석 서버에 연결할 수 없습니다.", 502);
  }
  return privateResponse("분석 서버에서 응답을 받지 못했습니다.", 502);
}
