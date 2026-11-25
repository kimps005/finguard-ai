import type { DecimalString, PriceSource, StockPriceBand } from "../lib/chat-stream.ts";

export interface LadderRow {
  key: "ceiling" | "sell" | "current" | "buy";
  label: string;
  hint: string;
  price: DecimalString;
  pct: DecimalString | null;
  reason: string;
  /** Undefined for the current-price row, which has no badge. */
  source: PriceSource | undefined;
}

export const sourceBadge = (source: PriceSource) => (source === "ai" ? "AI" : "기본 계산");

/** Four ladder rows, highest price first. Number() only orders rows; displayed values stay decimal strings. */
export function ladderRows(band: StockPriceBand): LadderRow[] {
  const { buy_zone: buy, sell_zone: sell, peak_limit: ceiling } = band.levels;
  const rows: LadderRow[] = [
    { key: "ceiling", label: "2주 상단", hint: "단기적으로 넘기 어려운 가격", price: ceiling.price, pct: ceiling.current_pct, reason: ceiling.reason, source: ceiling.source },
    { key: "sell", label: "단기 고점", hint: "차익 실현 검토", price: sell.price, pct: sell.current_pct, reason: sell.reason, source: sell.source },
    { key: "current", label: "현재가", hint: "분석 시점", price: band.current_price, pct: null, reason: "", source: undefined },
    { key: "buy", label: "단기 저점", hint: "매수 검토", price: buy.price, pct: buy.current_pct, reason: buy.reason, source: buy.source },
  ];
  return rows.sort((left, right) => Number(right.price) - Number(left.price));
}

/** Flow facts are share counts and already shown as 주 above, so the 원 price-fact list skips them. */
const SHARE_FACT_LABELS = new Set(["외국인 5일 순매수", "기관 5일 순매수"]);

export function priceFacts(band: StockPriceBand): StockPriceBand["facts"] {
  return band.facts.filter((fact) => !SHARE_FACT_LABELS.has(fact.label));
}
