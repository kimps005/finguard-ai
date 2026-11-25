export type DecimalString = `${number}`;

export interface Holding {
  stock_code: string;
  name: string;
  asset_type: string | null;
  average_price: DecimalString;
  profit_loss_pct: DecimalString;
}

export interface Portfolio {
  account_summary: {
    total_value: DecimalString;
    cash_deposit: DecimalString | null;
    cash_weight: DecimalString | null;
  };
  holdings: Holding[];
}

export interface AccountSnapshot extends Portfolio {
  account_summary: Portfolio["account_summary"] & {
    orderable_cash: DecimalString | null;
  };
}

export interface Status {
  node: string;
  phase: "started" | "completed" | "failed";
  task_id?: string;
  /** Sent only when a stock_worker task starts; later phases inherit it client-side. */
  stock_code?: string;
  stock_name?: string;
}

export type Scorecards = Record<string, {
  scores?: Record<string, number | null>;
  total?: number | null;
}>;

export interface Diagnosis extends Portfolio {
  ok: true;
  intent_type: "fast_lookup" | "deep_diagnosis" | "simulation";
  quant_scorecards: Scorecards;
  macro_context: Record<string, unknown> | null;
  simulation_result: Record<string, unknown> | null;
  verified_stock_results: unknown[];
  guardrail_warnings: string[];
  final_report: string;
}

interface PriceLevel {
  price: DecimalString;
  reason: string;
}

export type PriceSource = "ai" | "local";

export interface StockPriceLevel extends PriceLevel {
  current_pct: DecimalString;
  source: PriceSource;
  basis: string[];
}

interface BandIdentity {
  stock_code: string;
  name: string;
  current_price: DecimalString;
  average_price: DecimalString;
}

export type PriceBand = BandIdentity & ({ levels: null } | {
  levels: {
    buy: PriceLevel & { current_pct: DecimalString };
    sell: PriceLevel & { current_pct: DecimalString; average_pct: DecimalString | null };
    ceiling: PriceLevel;
  };
  position_pct: DecimalString;
  indicative: boolean;
  ma20: DecimalString | null;
  recent_range: { sessions: number; low: DecimalString; high: DecimalString } | null;
  flow: { foreign: DecimalString; institution: DecimalString } | null;
  disclosures: { alias: string; title: string; receipt: string }[];
});

export interface PriceBands {
  as_of: string;
  bands: PriceBand[];
}

export interface StockSearchResult {
  stock_code: string;
  corp_name: string;
  market: string | null;
}

export type StockPriceBand = Omit<Extract<PriceBand, { levels: object }>, "average_price" | "levels"> & {
  change_pct: DecimalString | null;
  levels: Record<"buy_zone" | "sell_zone" | "peak_limit", StockPriceLevel>;
  horizon_sessions: number;
  summary: string;
  risks: { text: string; basis: string[] }[];
  news: { alias: string; title: string; link: string }[];
  facts: { id: string; label: string; value: DecimalString; upper: DecimalString | null; session: string | null }[];
  ai_status: "ai" | "partial" | "local";
  warnings: string[];
};

export interface StockPriceBands {
  as_of: string;
  band: StockPriceBand;
}

type BandSnapshot = PriceBands | StockPriceBands;

type ChatEvent =
  | { event: "status"; data: Status }
  | { event: "token"; data: { text: string } }
  | { event: "price_bands"; data: BandSnapshot }
  | { event: "error"; data: { message: string } }
  | { event: "done"; data: Diagnosis | (BandSnapshot & { ok: true; mode: "price_bands" }) | { ok: false } };

export interface ChatRequest {
  user_query: string;
  /** The stock whose price card is on screen; the answer covers it even when it is not held. */
  selected_stock_code?: string;
}

export function chatRequest(user_query: string, selected_stock_code?: string): ChatRequest {
  return selected_stock_code ? { user_query, selected_stock_code } : { user_query };
}

/** The access PIN was rejected or has expired; the caller should lock the dashboard. */
export class SessionExpired extends Error {}

/** POST to the PIN-protected chat proxy and yield its SSE events. */
export async function* streamChat(payload: object, { pin, pinHeader, signal }: {
  pin: string; pinHeader: string; signal: AbortSignal;
}, startError: string): AsyncGenerator<ChatEvent> {
  const response = await fetch("/api/chat", {
    method: "POST",
    headers: { [pinHeader]: pin, "Content-Type": "application/json" },
    body: JSON.stringify(payload),
    signal,
  });
  if (response.status === 401) throw new SessionExpired("접근이 만료되었습니다. PIN을 다시 입력해 주세요.");
  if (!response.ok || !response.body) throw new Error(startError);
  yield* readChatEvents(response.body);
}

export async function* readChatEvents(body: ReadableStream<Uint8Array>): AsyncGenerator<ChatEvent> {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let pending = "";
  let event = "";
  let lines: string[] = [];

  try {
    while (true) {
      const chunk = await reader.read();
      pending += decoder.decode(chunk.value, { stream: !chunk.done });
      let start = 0;
      const endings = /\r\n|[\r\n]/g;
      for (const ending of pending.matchAll(endings)) {
        const end = ending.index!;
        if (!chunk.done && ending[0] === "\r" && end === pending.length - 1) break;
        const line = pending.slice(start, end);
        start = end + ending[0].length;
        if (line === "") {
          if (lines.length && ["status", "token", "price_bands", "error", "done"].includes(event)) {
            yield { event, data: JSON.parse(lines.join("\n")) } as ChatEvent;
          }
          event = "";
          lines = [];
        } else {
          const separator = line.indexOf(":");
          const field = separator < 0 ? line : line.slice(0, separator);
          const value = separator < 0 ? "" : line.slice(separator + 1).replace(/^ /, "");
          if (field === "event") event = value;
          if (field === "data") lines.push(value);
        }
      }
      pending = pending.slice(start);
      if (chunk.done) break;
    }
  } finally {
    void reader.cancel().catch(() => {});
    reader.releaseLock();
  }
}
