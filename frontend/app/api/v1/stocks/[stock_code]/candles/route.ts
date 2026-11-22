import { fetchBackend, hasValidPin, privateResponse } from "../../../../../../lib/backend-proxy.ts";

export const dynamic = "force-dynamic";

export async function GET(request: Request, { params }: { params: { stock_code: string } }): Promise<Response> {
  if (!hasValidPin(request)) return privateResponse("Unauthorized", 401);
  const { stock_code } = params;
  const interval = new URL(request.url).searchParams.get("interval") ?? "10m";
  if (!/^[0-9A-Za-z]{6}$/.test(stock_code) || !["10m", "1d", "1w", "1y"].includes(interval)) {
    return privateResponse("종목 코드와 봉 간격을 확인해 주세요.", 422);
  }

  try {
    const upstream = await fetchBackend(request, `/api/v1/stocks/${stock_code}/candles?interval=${interval}`);
    const pollAfter = upstream.headers.get("X-Candle-Poll-After");
    if (upstream.ok && upstream.body) {
      const headers = new Headers({ "Content-Type": "application/json", "Cache-Control": "no-store" });
      if (pollAfter) headers.set("X-Candle-Poll-After", pollAfter);
      return new Response(upstream.body, { headers });
    }
    await upstream.body?.cancel();
    const unavailable = privateResponse("가격 차트를 불러올 수 없습니다.", 502);
    if (pollAfter) unavailable.headers.set("X-Candle-Poll-After", pollAfter);
    return unavailable;
  } catch {
    return privateResponse("가격 차트 서버에 연결할 수 없습니다.", 502);
  }
}
