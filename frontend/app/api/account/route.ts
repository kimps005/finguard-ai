import { fetchBackend, hasValidPin, privateResponse } from "../../../lib/backend-proxy.ts";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

const accountSettings = {
  live: "한국투자증권 실전 계좌 설정이 필요합니다. .env의 KIS_ACCOUNT_NO, KIS_APP_KEY, KIS_APP_SECRET을 확인해 주세요.",
  virtual: "한국투자증권 모의 계좌 설정이 필요합니다. .env의 KIS_VIRTUAL_ACCOUNT_NO, KIS_VIRTUAL_APP_KEY, KIS_VIRTUAL_APP_SECRET을 확인해 주세요.",
};

const tossSettings = new Map([
  ["toss_account_not_configured", "토스증권 계좌 연결이 필요합니다. .env에 TOSS_CLIENT_ID와 TOSS_CLIENT_SECRET을 입력한 뒤 백엔드를 다시 시작해 주세요."],
  ["toss_account_selection_required", "토스증권 종합매매 계좌를 선택할 수 없습니다. 연결된 계좌를 확인하고, 계좌가 여러 개면 .env에 TOSS_ACCOUNT_SEQ를 지정해 주세요."],
  ["toss_access_denied", "토스증권이 계좌 조회를 거절했습니다. API 키와 계좌 조회 권한을 확인해 주세요."],
]);

export async function GET(request: Request): Promise<Response> {
  if (!hasValidPin(request)) return privateResponse("Unauthorized", 401);

  try {
    const upstream = await fetchBackend(request, "/api/v1/account");
    const isJson = upstream.headers.get("Content-Type")?.split(";")[0].trim() === "application/json";
    if (upstream.ok && upstream.body && isJson) {
      return new Response(upstream.body, {
        headers: { "Content-Type": "application/json", "Cache-Control": "no-store" },
      });
    }
    if (upstream.status === 503 && isJson) {
      const problem: { code?: unknown; mode?: unknown } | null = await upstream.json().catch(() => null);
      if (problem?.code === "kis_account_not_configured" &&
          (problem.mode === "live" || problem.mode === "virtual")) {
        return privateResponse(accountSettings[problem.mode], 503);
      }
      if (typeof problem?.code === "string") {
        const message = tossSettings.get(problem.code);
        if (message) return privateResponse(message, 503);
      }
    } else {
      await upstream.body?.cancel();
    }
  } catch {
    return privateResponse("계좌 서버에 연결할 수 없습니다.", 502);
  }
  return privateResponse("계좌 정보를 불러올 수 없습니다.", 502);
}
