import { forwardJson, hasValidPin, privateResponse } from "../../../../lib/backend-proxy.ts";

export const dynamic = "force-dynamic";

export async function GET(request: Request): Promise<Response> {
  if (!hasValidPin(request)) return privateResponse("Unauthorized", 401);
  const query = new URL(request.url).searchParams.get("q")?.trim() ?? "";
  if (!query || query.length > 100) return privateResponse("검색어를 확인해 주세요.", 422);
  return forwardJson(request, "/api/v1/stocks/search?" + new URLSearchParams({ q: query }), {
    failure: "종목 검색을 완료할 수 없습니다.",
    unreachable: "종목 검색 서버에 연결할 수 없습니다.",
  });
}
