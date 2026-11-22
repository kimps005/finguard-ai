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

type ChatEvent =
  | { event: "status"; data: Status }
  | { event: "token"; data: { text: string } }
  | { event: "price_bands"; data: PriceBands }
  | { event: "error"; data: { message: string } }
  | { event: "done"; data: Diagnosis | (PriceBands & { ok: true; mode: "price_bands" }) | { ok: false } };

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
