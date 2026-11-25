import assert from "node:assert/strict";
import test from "node:test";
import { ladderRows, sourceBadge } from "../app/price-card-rows.ts";
import type { DecimalString, StockPriceBand } from "../lib/chat-stream.ts";

type Source = "ai" | "local";
const level = (price: DecimalString, source: Source) => ({ price, reason: "근거", current_pct: "1.5" as DecimalString, source, basis: ["P-1"] });
const band = (sources: [Source, Source, Source] = ["ai", "ai", "ai"]): StockPriceBand => ({
  stock_code: "005930", name: "삼성전자", current_price: "70000", change_pct: "0.5", position_pct: "50",
  indicative: false, ma20: null, recent_range: null, flow: null, disclosures: [],
  horizon_sessions: 10, summary: "", risks: [], news: [], facts: [], ai_status: "ai", warnings: [],
  levels: { buy_zone: level("66000", sources[0]), sell_zone: level("73000", sources[1]), peak_limit: level("76000", sources[2]) },
});

test("rows use the fixed four labels in descending price order", () => {
  const rows = ladderRows(band());
  assert.deepEqual(rows.map((row) => row.label), ["2주 상단", "단기 고점", "현재가", "단기 저점"]);
  assert.deepEqual(rows.map((row) => row.price), ["76000", "73000", "70000", "66000"]);
  assert.equal(rows[1].hint, "차익 실현 검토");
  assert.equal(rows[2].hint, "분석 시점");
  assert.equal(rows[3].hint, "매수 검토");
});

test("rows re-sort when levels cross the current price", () => {
  const crossed = band();
  crossed.levels.buy_zone.price = "71000";
  assert.deepEqual(ladderRows(crossed).map((row) => row.label), ["2주 상단", "단기 고점", "단기 저점", "현재가"]);
});

test("badge text maps the source and the current row has none", () => {
  const rows = ladderRows(band(["local", "ai", "local"]));
  assert.deepEqual(rows.map((row) => row.source), ["local", "ai", undefined, "local"]);
  assert.equal(sourceBadge("ai"), "AI");
  assert.equal(sourceBadge("local"), "기본 계산");
});

test("a local card shows the basic-calculation badge on every level", () => {
  const rows = ladderRows({ ...band(["local", "local", "local"]), ai_status: "local" });
  assert.deepEqual(rows.filter((row) => row.key !== "current").map((row) => sourceBadge(row.source!)),
    ["기본 계산", "기본 계산", "기본 계산"]);
});
