import assert from "node:assert/strict";
import test from "node:test";
import { analysisReducer, idleRun, type AnalysisAction } from "../lib/analysis-run.ts";

const run = (actions: AnalysisAction[], state = idleRun) => actions.reduce(analysisReducer, state);

test("a new question clears the previous run but keeps nothing stale", () => {
  const previous = run([{ type: "start", topic: "잔고" }, { type: "token", text: "이전" }, { type: "fail", message: "오류" },
    { type: "done", report: "이전 리포트", warnings: ["경고"], elapsed: 1200 }]);
  assert.deepEqual(analysisReducer(previous, { type: "start", topic: "리스크" }), { ...idleRun, topic: "리스크", running: true });
});

test("statuses merge per task, move the latest update last, and keep earlier metadata", () => {
  const state = run([
    { type: "start", topic: "진단" },
    { type: "status", status: { node: "router_node", phase: "started" } },
    { type: "status", status: { node: "stock_worker", phase: "started", task_id: "a", stock_code: "005930" } },
    { type: "status", status: { node: "stock_worker", phase: "started", task_id: "b" } },
    { type: "status", status: { node: "router_node", phase: "completed", task_id: "router-run" } },
    { type: "status", status: { node: "stock_worker", phase: "completed", task_id: "a" } },
  ]);
  assert.deepEqual(Object.keys(state.statuses), ["b", "router_node", "a"]);
  assert.deepEqual(state.statuses.a, { node: "stock_worker", phase: "completed", task_id: "a", stock_code: "005930" });
  assert.equal(state.statuses.router_node.phase, "completed");
});

test("tokens stream into the report until done replaces it with the final text", () => {
  const streaming = run([{ type: "start", topic: "잔고" }, { type: "token", text: "총 " }, { type: "token", text: "평가액" }]);
  assert.equal(streaming.report, "총 평가액");
  const done = run([{ type: "done", report: "최종", warnings: ["참고"], elapsed: 900 }, { type: "finish" }], streaming);
  assert.deepEqual([done.report, done.warnings, done.elapsed, done.running], ["최종", ["참고"], 900, false]);
});

test("reset returns to the idle panel", () => {
  assert.equal(run([{ type: "start", topic: "x" }, { type: "reset" }]), idleRun);
});
