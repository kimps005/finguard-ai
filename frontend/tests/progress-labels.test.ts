import assert from "node:assert/strict";
import test from "node:test";
import type { AccountSnapshot } from "../lib/chat-stream.ts";
import { describeTask } from "../lib/progress-labels.ts";

const holdings = [{ stock_code: "018880", name: "한온시스템" }] as AccountSnapshot["holdings"];

test("digits inside a task UUID are never read as a stock code", () => {
  for (const task_id of ["7f3a9195-8812-0186-018880-aa", "919588ab-0000-4000-8000-000000000000"]) {
    assert.equal(describeTask({ node: "router_node", phase: "started", task_id }, holdings), "질문 의도와 분석 전략 설정");
  }
});

test("worker steps name the stock the server sent", () => {
  const worker = { node: "stock_worker", phase: "started" as const, task_id: "t", stock_code: "018880" };
  assert.equal(describeTask({ ...worker, stock_name: "한온시스템" }, []), "한온시스템(018880) 공시·수급 분석");
  // Without a name, the holding list fills it in; an unknown code still shows the code.
  assert.equal(describeTask(worker, holdings), "한온시스템(018880) 공시·수급 분석");
  assert.equal(describeTask({ ...worker, stock_code: "005930" }, holdings), "005930 공시·수급 분석");
  assert.equal(describeTask({ node: "stock_worker", phase: "started" }, holdings), "보유 종목 DART 공시·재무 리스크 분석");
});
