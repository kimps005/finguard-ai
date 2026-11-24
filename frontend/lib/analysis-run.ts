import type { Status } from "./chat-stream";

/** Everything the diagnosis panel shows for one question, from submission to the done event. */
export interface AnalysisRun {
  topic: string;
  running: boolean;
  elapsed: number | null;
  statuses: Record<string, Status>;
  report: string;
  warnings: string[];
  error: string;
}

export type AnalysisAction =
  | { type: "start"; topic: string }
  | { type: "status"; status: Status }
  | { type: "token"; text: string }
  | { type: "done"; report: string; warnings: string[]; elapsed: number }
  | { type: "fail"; message: string }
  | { type: "finish" }
  | { type: "reset" };

export const idleRun: AnalysisRun = {
  topic: "", running: false, elapsed: null, statuses: {}, report: "", warnings: [], error: "",
};

export function analysisReducer(state: AnalysisRun, action: AnalysisAction): AnalysisRun {
  switch (action.type) {
    case "start":
      return { ...idleRun, topic: action.topic, running: true };
    case "status": {
      const { status } = action;
      // The router has one task; workers are keyed per task so parallel stocks stay separate.
      const key = status.node === "router_node" ? status.node : status.task_id ?? status.node;
      // Re-insert the key so the latest update sorts last; keep metadata a later phase omits.
      const { [key]: previous, ...rest } = state.statuses;
      return { ...state, statuses: { ...rest, [key]: { ...previous, ...status } } };
    }
    case "token":
      return { ...state, report: state.report + action.text };
    case "done":
      return { ...state, report: action.report, warnings: action.warnings, elapsed: action.elapsed };
    case "fail":
      return { ...state, error: action.message };
    case "finish":
      return { ...state, running: false };
    case "reset":
      return idleRun;
  }
}
