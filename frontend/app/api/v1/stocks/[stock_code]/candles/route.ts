import { forwardJson, hasValidPin, privateResponse } from "../../../../../../lib/backend-proxy.ts";

export const dynamic = "force-dynamic";

export async function GET(request: Request, { params }: { params: { stock_code: string } }): Promise<Response> {
  if (!hasValidPin(request)) return privateResponse("Unauthorized", 401);
  const { stock_code } = params;
  const interval = new URL(request.url).searchParams.get("interval") ?? "10m";
  if (!/^[0-9A-Za-z]{6}$/.test(stock_code) || !["10m", "1d", "1w", "1y"].includes(interval)) {
    return privateResponse("종목 코드와 봉 간격을 확인해 주세요.", 422);
  }
  // The poll hint also rides on failures so the chart knows when to retry.
  return forwardJson(request, `/api/v1/stocks/${stock_code}/candles?interval=${interval}`, {
    failure: "가격 차트를 불러올 수 없습니다.",
    unreachable: "가격 차트 서버에 연결할 수 없습니다.",
  }, ["X-Candle-Poll-After"]);
}
