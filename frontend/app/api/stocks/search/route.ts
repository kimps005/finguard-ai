import { fetchBackend, hasValidPin, privateResponse } from "../../../../lib/backend-proxy.ts";

export const dynamic = "force-dynamic";

export async function GET(request: Request): Promise<Response> {
  if (!hasValidPin(request)) return privateResponse("Unauthorized", 401);
  const query = new URL(request.url).searchParams.get("q")?.trim() ?? "";
  if (!query || query.length > 100) return privateResponse("검색어를 확인해 주세요.", 422);

  try {
    const upstream = await fetchBackend(request, "/api/v1/stocks/search?" + new URLSearchParams({ q: query }));
    if (upstream.ok && upstream.body) {
      return new Response(upstream.body, {
        headers: { "Content-Type": "application/json", "Cache-Control": "no-store" },
      });
    }
    await upstream.body?.cancel();
  } catch {
    return privateResponse("종목 검색 서버에 연결할 수 없습니다.", 502);
  }
  return privateResponse("종목 검색을 완료할 수 없습니다.", 502);
}
