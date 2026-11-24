import type { AccountSnapshot, Status } from "./chat-stream";

export const stageNames: Record<string, string> = {
  router_node: "질문 의도와 분석 전략 설정",
  fast_quote_node: "계좌 잔고와 실시간 시세 조회",
  market_data_node: "실시간 시세·20일선·수급 데이터 수집",
  quant_scorecard_node: "보유 종목 리스크 점수 계산",
  stock_worker: "보유 종목 DART 공시·재무 리스크 분석",
  guardrail_node: "공시 인용 및 수치 교차 검증",
  synthesizer_node: "맞춤 대응 리포트 작성",
};

export function displayField(payload: object, keys: string[]) {
  const fields = payload as Record<string, unknown>;
  return keys.map((key) => fields[key]).find((value): value is string =>
    typeof value === "string" && value.trim().length > 0)?.trim();
}

export function describeTask(status: Status, holdings: AccountSnapshot["holdings"]) {
  // Only the stock fields the server sends identify a stock; task ids are opaque UUIDs.
  const code = displayField(status, ["stock_code", "ticker"]);
  const holding = code ? holdings.find((item) => item.stock_code === code) : undefined;
  const name = displayField(status, ["stock_name", "name"]) ?? holding?.name;
  const ticker = code ?? holding?.stock_code;
  const subject = name && ticker ? `${name}(${ticker})` : name ?? ticker;
  const detail = displayField(status, ["task_name", "detail", "message"]);
  if (subject) return `${subject} ${detail ?? (status.node === "stock_worker" ? "공시·수급 분석" : stageNames[status.node] ?? "분석")}`;
  return detail ?? stageNames[status.node] ?? "계좌 데이터 분석";
}
