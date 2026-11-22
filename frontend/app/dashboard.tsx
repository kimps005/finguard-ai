"use client";

import { useCallback, useEffect, useRef, useState, type FormEvent } from "react";
import {
  Activity, ArrowRight, ArrowUpRight, Check, CircleAlert, FileText,
  Globe2, Layers3, LoaderCircle, LockKeyhole, Send, ShieldCheck, Wallet,
} from "lucide-react";
import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { readChatEvents, type AccountSnapshot, type DecimalString, type Scorecards, type Status } from "../lib/chat-stream";
import PriceOutlook from "./price-outlook";

const PIN_KEY = "finguard-app-pin";
const PIN_HEADER = "X-App-Pin";
const stageNames: Record<string, string> = {
  router_node: "질문 의도와 분석 전략 설정",
  fast_quote_node: "계좌 잔고와 실시간 시세 조회",
  market_data_node: "실시간 시세·20일선·수급 데이터 수집",
  quant_scorecard_node: "보유 종목 리스크 점수 계산",
  stock_worker: "보유 종목 DART 공시·재무 리스크 분석",
  guardrail_node: "공시 인용 및 수치 교차 검증",
  synthesizer_node: "맞춤 대응 리포트 작성",
};

const won = new Intl.NumberFormat("ko-KR", { maximumFractionDigits: 2 });
const percent = new Intl.NumberFormat("ko-KR", { style: "percent", maximumFractionDigits: 2 });

function holdingReturn(value: DecimalString) {
  const negative = value.startsWith("-");
  const nonzero = /[1-9]/.test(value);
  return {
    direction: nonzero ? (negative ? "loss" : "gain") : "flat",
    text: nonzero ? `${negative ? "" : "+"}${won.format(value)}%` : "0%",
  };
}

function displayField(payload: object, keys: string[]) {
  const fields = payload as Record<string, unknown>;
  return keys.map((key) => fields[key]).find((value): value is string =>
    typeof value === "string" && value.trim().length > 0)?.trim();
}

function describeTask(status: Status, holdings: AccountSnapshot["holdings"]) {
  const code = displayField(status, ["stock_code", "ticker"])
    ?? status.task_id?.match(/(?:^|\D)(\d{6})(?!\d)/)?.[1];
  const holding = holdings.find((item) => item.stock_code === code || item.name === status.task_id);
  const name = displayField(status, ["stock_name", "name"]) ?? holding?.name;
  const ticker = code ?? holding?.stock_code;
  const subject = name && ticker ? `${name}(${ticker})` : name ?? ticker;
  const detail = displayField(status, ["task_name", "detail", "message"]);
  if (subject) return `${subject} ${detail ?? (status.node === "stock_worker" ? "공시·수급 분석" : stageNames[status.node] ?? "분석")}`;
  return detail ?? stageNames[status.node] ?? "계좌 데이터 분석";
}

function questionTopic(question: string, holdings: AccountSnapshot["holdings"]) {
  const subject = holdings.find((holding) => question.includes(holding.name) || question.includes(holding.stock_code))?.name;
  const topics = [
    [/잔고|수익률|평가액|손익|계좌 조회/u, "잔고·수익률"],
    [/공시|오버행|재무|희석/u, "공시·재무 리스크"],
    [/수급|시세|20일선/u, "시세·수급"],
    [/환율|금리|매크로/u, "환율·금리"],
    [/물타기|시뮬레이션|추가\s*매수/u, "추가매수 시뮬레이션"],
    [/비중|리밸런싱/u, "포트폴리오 비중"],
    [/전략|대응|매도/u, "대응 전략"],
  ] as const;
  const keywords = topics.filter(([pattern]) => pattern.test(question)).slice(0, 2).map(([, label]) => label);
  return [subject, ...keywords].filter(Boolean).join(" · ") || "포트폴리오 진단";
}

function AnalysisProgress({ statuses, holdings, elapsed }: {
  statuses: Status[];
  holdings: AccountSnapshot["holdings"];
  elapsed: number | null;
}) {
  if (elapsed !== null) {
    const seconds = Math.max(1, Math.round(elapsed / 1000));
    const duration = seconds < 60 ? `${seconds}초` : `${Math.floor(seconds / 60)}분 ${seconds % 60}초`;
    return (
      <div className="progress-complete" role="status">
        <span className="completion-icon"><Check size={17} aria-hidden="true" /></span>
        <span>분석 완료</span>
        <span className="duration">{duration}</span>
      </div>
    );
  }

  const active = statuses.filter((status) => status.phase === "started").reverse();
  const current = active[0] ?? statuses[statuses.length - 1];
  const related = [...new Set(active.slice(1).map((status) => describeTask(status, holdings)))];
  const currentTitle = current ? describeTask(current, holdings) : stageNames.router_node;
  const title = active.length || !current ? `${currentTitle} 중` : `${currentTitle} ${current.phase === "failed" ? "실패" : "완료"}`;
  return (
    <div className="live-progress">
      <div className="live-stage">
        <span className="radar" aria-hidden="true"><span className="animate-ping motion-reduce:animate-none" /></span>
        <div className="min-w-0" role="status">
          <p className="live-label">실시간 분석</p>
          <p className="live-title">{title}</p>
          {related.some((detail) => detail !== currentTitle) && (
            <ul className="live-tasks">
              {related.filter((detail) => detail !== currentTitle).map((detail) => <li key={detail}>{detail} 중</li>)}
            </ul>
          )}
        </div>
      </div>
      <div className="progress-track" aria-hidden="true" />
    </div>
  );
}

function ReportBody({ report }: { report: string }) {
  const evidenceHeading = /^(?:#{1,6}[ \t]+|\*\*)(?:\d+[.)][ \t]*)?(?:판단 근거|핵심 근거|근거 공시|공시 근거|근거|출처|인용 공시)(?:[ \t]*\*\*)?[ \t]*[:：]?[ \t]*#*[ \t]*$/mu;
  const evidence = evidenceHeading.exec(report);
  const answer = (evidence ? report.slice(0, evidence.index) : report)
    .replace(/^(?:\s*#{1,6}[ \t]+|\s*\*\*)핵심 답변(?:\*\*)?[ \t]*[:：]?[ \t]*#*[ \t]*\r?\n/u, "")
    .trim();
  const sources = evidence ? report.slice(evidence.index + evidence[0].length).trim() : "";
  const sections = [{ title: "핵심 답변", content: answer }, { title: "판단 근거", content: sources }];
  return sections.filter(({ content }) => content).map(({ title, content }) => (
    <div className="report-section" key={title}>
      <h3>{title}</h3>
      <div className="markdown">
        <Markdown remarkPlugins={[remarkGfm]} disallowedElements={["img"]}
          components={{ table: ({ children }) => <div className="table-scroll"><table>{children}</table></div> }}>
          {content}
        </Markdown>
      </div>
    </div>
  ));
}

export default function Dashboard() {
  const [pin, setPin] = useState("");
  const [pinInput, setPinInput] = useState("");
  const [checking, setChecking] = useState(true);
  const [accessError, setAccessError] = useState("");
  const [account, setAccount] = useState<AccountSnapshot | null>(null);
  const [accountError, setAccountError] = useState("");
  const [scores, setScores] = useState<Scorecards>({});
  const [simulationCode, setSimulationCode] = useState("");
  const [query, setQuery] = useState("");
  const [topic, setTopic] = useState("");
  const [running, setRunning] = useState(false);
  const [elapsed, setElapsed] = useState<number | null>(null);
  const [statuses, setStatuses] = useState<Record<string, Status>>({});
  const [report, setReport] = useState("");
  const [warnings, setWarnings] = useState<string[]>([]);
  const [analysisError, setAnalysisError] = useState("");
  const requestRef = useRef<AbortController | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    const stored = sessionStorage.getItem(PIN_KEY);
    if (stored) void unlock(stored, controller.signal);
    else setChecking(false);
    return () => {
      controller.abort();
      requestRef.current?.abort();
    };
  }, []);

  const lock = useCallback((message = "") => {
    requestRef.current?.abort();
    requestRef.current = null;
    sessionStorage.removeItem(PIN_KEY);
    setPin("");
    setPinInput("");
    setAccount(null);
    setAccountError("");
    setScores({});
    setSimulationCode("");
    setQuery("");
    setTopic("");
    setReport("");
    setStatuses({});
    setWarnings([]);
    setAnalysisError("");
    setAccessError(message);
    setChecking(false);
    setRunning(false);
    setElapsed(null);
  }, []);

  useEffect(() => {
    if (!pin) return;
    const controller = new AbortController();
    async function loadAccount() {
      try {
        const response = await fetch("/api/account", { headers: { [PIN_HEADER]: pin }, signal: controller.signal });
        if (response.status === 401) {
          lock("접근이 만료되었습니다. PIN을 다시 입력해 주세요.");
          return;
        }
        if (!response.ok) {
          const problem = await response.json().catch(() => null);
          throw new Error(typeof problem?.message === "string" ? problem.message : "계좌 정보를 불러올 수 없습니다.");
        }
        const snapshot: AccountSnapshot = await response.json();
        if (!controller.signal.aborted) setAccount((current) => current ? {
          ...current,
          account_summary: { ...current.account_summary, orderable_cash: snapshot.account_summary.orderable_cash },
        } : snapshot);
      } catch (error) {
        if (!controller.signal.aborted) setAccountError(
          error instanceof Error ? error.message : "계좌 정보를 불러올 수 없습니다.");
      }
    }
    void loadAccount();
    return () => controller.abort();
  }, [lock, pin]);

  async function unlock(candidate: string, signal: AbortSignal) {
    setChecking(true);
    setAccessError("");
    try {
      const response = await fetch("/api/account", { method: "HEAD", headers: { [PIN_HEADER]: candidate }, signal });
      if (signal.aborted) return;
      if (response.status === 401) {
        lock("접근 PIN을 확인해 주세요.");
        return;
      }
      if (!response.ok) throw new Error("접근을 확인할 수 없습니다. 잠시 후 다시 시도해 주세요.");
      sessionStorage.setItem(PIN_KEY, candidate);
      setPin(candidate);
      setPinInput("");
    } catch (error) {
      if (!signal.aborted) {
        setAccessError(error instanceof Error ? error.message : "대시보드에 연결할 수 없습니다.");
      }
    } finally {
      if (!signal.aborted) setChecking(false);
    }
  }

  function submitPin(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (checking || !pinInput.trim()) return;
    const controller = new AbortController();
    requestRef.current = controller;
    void unlock(pinInput.trim(), controller.signal).finally(() => {
      if (requestRef.current === controller) requestRef.current = null;
    });
  }

  async function analyze(value: string, label = questionTopic(value, account?.holdings ?? [])) {
    const question = value.trim();
    if (!pin || !question || requestRef.current) return;
    const controller = new AbortController();
    const startedAt = performance.now();
    requestRef.current = controller;
    setTopic(label);
    setRunning(true);
    setElapsed(null);
    setReport("");
    setStatuses({});
    setWarnings([]);
    setAnalysisError("");

    try {
      const response = await fetch("/api/chat", {
        method: "POST",
        headers: { [PIN_HEADER]: pin, "Content-Type": "application/json" },
        body: JSON.stringify({ user_query: question }),
        signal: controller.signal,
      });
      if (response.status === 401) {
        lock("접근이 만료되었습니다. PIN을 다시 입력해 주세요.");
        return;
      }
      if (!response.ok || !response.body) throw new Error("진단을 시작할 수 없습니다. 다시 시도해 주세요.");

      for await (const item of readChatEvents(response.body)) {
        if (item.event === "status") {
          const status = item.data;
          const key = status.node === "router_node" ? status.node : status.task_id ?? status.node;
          setStatuses((current) => {
            // Keep event order and retain target metadata when a later phase omits it.
            const next = { ...current };
            delete next[key];
            return { ...next, [key]: { ...current[key], ...status } };
          });
        } else if (item.event === "token") {
          setReport((current) => current + item.data.text);
        } else if (item.event === "error") {
          throw new Error(item.data.message);
        } else if (item.event === "done") {
          const result = item.data;
          if (!result.ok || !("final_report" in result)) throw new Error("계좌 분석을 완료할 수 없습니다.");
          setReport(result.final_report);
          setAccountError("");
          setWarnings(result.guardrail_warnings);
          setAccount((current) => ({
            holdings: result.holdings,
            account_summary: {
              ...result.account_summary,
              orderable_cash: current?.account_summary.orderable_cash ?? null,
            },
          }));
          if (result.intent_type !== "fast_lookup") setScores(result.quant_scorecards);
          setElapsed(performance.now() - startedAt);
          return;
        }
      }
      throw new Error("분석 스트림이 완료되기 전에 연결이 끊겼습니다.");
    } catch (error) {
      if (!controller.signal.aborted) {
        setAnalysisError(error instanceof Error ? error.message : "계좌 분석을 완료할 수 없습니다.");
      }
    } finally {
      if (requestRef.current === controller) {
        requestRef.current = null;
        setRunning(false);
      }
    }
  }

  const summary = account?.account_summary;
  const accountReturn = summary && displayField(summary, ["profit_loss_pct", "total_profit_loss_pct", "return_pct"]);
  const accountChange = accountReturn && /^[+-]?\d+(?:\.\d+)?$/u.test(accountReturn)
    ? holdingReturn(accountReturn as DecimalString)
    : null;
  const holdingOptions = account?.holdings ?? [];
  const questionDisabled = running;
  const simulationHolding = holdingOptions.find((holding) => holding.stock_code === simulationCode)
    ?? holdingOptions.find((holding) => holding.profit_loss_pct.startsWith("-"))
    ?? holdingOptions[0];

  return (
    <>
      <header className="topbar">
        <div className="shell flex items-center justify-between">
          <div className="flex items-center gap-3">
            <span className="brand-icon"><ShieldCheck size={20} /></span>
            <span className="brand">FinGuard <span>AI</span></span>
          </div>
          {pin && <button className="lock-button" onClick={() => lock()}><LockKeyhole size={15} /> 잠그기</button>}
        </div>
      </header>
      {!pin ? (
        <main className="lock-page">
          <form className="panel lock-panel" onSubmit={submitPin}>
            <span className="section-icon"><LockKeyhole size={23} /></span>
            <h1>대시보드 잠금 해제</h1>
            <p className="muted">대시보드를 열려면 접근 PIN을 입력하세요.</p>
            <label htmlFor="pin">접근 PIN</label>
            <input id="pin" type="password" value={pinInput} onChange={(event) => setPinInput(event.target.value)}
              placeholder="PIN 입력" autoComplete="off" maxLength={128} required />
            {accessError && <p className="error" role="alert">{accessError}</p>}
            <button className="primary-button" disabled={checking || !pinInput.trim()}>
              {checking ? "확인 중" : "잠금 해제"} {checking ? <LoaderCircle size={16} className="animate-spin motion-reduce:animate-none" /> : <ArrowRight size={16} />}
            </button>
          </form>
        </main>
      ) : (
        <main className="shell dashboard">
          <PriceOutlook pin={pin} pinHeader={PIN_HEADER} holdings={holdingOptions} onUnauthorized={lock} />
          {accountError && <p className="outlook-note mb-4" role="status">{accountError}</p>}
          <div className={summary ? "dashboard-grid" : undefined}>
            {summary && <aside className="min-w-0 space-y-5" aria-label="계좌와 보유 종목">
              <section className="panel account-panel" aria-labelledby="account-title">
                <h2 id="account-title"><Wallet size={17} /> 계좌 요약</h2>
                <dl>
                  <div className="account-total">
                    <dt>{summary.cash_deposit === null ? "국내 주식 평가액" : "총 평가액"}</dt>
                    <dd>{won.format(summary.total_value)}<small>원</small></dd>
                  </div>
                  {accountChange && <div className="account-return">
                    <dt>평가손익률</dt><dd className="return-badge" data-direction={accountChange.direction}>{accountChange.text}</dd>
                  </div>}
                  {(summary.orderable_cash !== null || summary.cash_weight !== null) && (
                    <div className="account-cash">
                      {summary.orderable_cash !== null && (
                        <div><dt>매수 가능 금액</dt><dd>{won.format(summary.orderable_cash)}원</dd></div>
                      )}
                      {summary.cash_weight !== null && (
                        <div><dt>현금 비중</dt><dd>{percent.format(summary.cash_weight)}</dd></div>
                      )}
                    </div>
                  )}
                </dl>
              </section>
              <section className="panel holdings-panel" aria-labelledby="holdings-title">
                <div className="section-heading">
                  <h2 id="holdings-title"><Layers3 size={19} /> 보유 종목</h2>
                  <span className="muted text-sm">{holdingOptions.length}종목</span>
                </div>
                {holdingOptions.length === 0 ? <p className="empty-holdings muted">보유 종목이 없습니다.</p> : (
                  <ul>
                    {holdingOptions.map((holding) => {
                      const change = holdingReturn(holding.profit_loss_pct);
                      const card = scores[holding.stock_code];
                      const score = holding.asset_type === "STOCK" ? card?.total : null;
                      return (
                        <li key={holding.stock_code} className="holding">
                          <div className="holding-heading">
                            <div className="min-w-0">
                              <h3>{holding.name}</h3>
                              <span className="ticker">{holding.stock_code}</span>
                            </div>
                            <span className="return-badge" data-direction={change.direction} aria-label={`평가손익률 ${change.text}`}>
                              {change.text}
                            </span>
                          </div>
                          <dl className="holding-metrics">
                            <div><dt>평단가</dt><dd>{won.format(holding.average_price)}원</dd></div>
                            {score != null && <div><dt>정량 점수</dt><dd>{score}/100</dd></div>}
                          </dl>
                        </li>
                      );
                    })}
                  </ul>
                )}
              </section>
            </aside>}
            <div className="min-w-0 space-y-5">
              <section className="panel analysis-panel" aria-labelledby="diagnosis-title">
                <div className="section-heading"><h2 id="diagnosis-title"><Activity size={19} /> 직접 질문하기</h2></div>
                <form className="query-form" onSubmit={(event) => {
                  event.preventDefault();
                  if (questionDisabled || !query.trim()) return;
                  void analyze(query);
                  setQuery("");
                }}>
                  <label className="sr-only" htmlFor="query">계좌 진단 질문</label>
                  <input id="query" maxLength={4000} placeholder="예: 내 계좌의 현재 잔고와 수익률은?" value={query}
                    onChange={(event) => setQuery(event.target.value)} />
                  <button className="primary-button" aria-label="질문 보내기" disabled={questionDisabled || !query.trim()}><Send size={17} /></button>
                </form>
                <h3 id="quick-actions-title" className="mt-5 text-sm font-semibold">빠른 진단</h3>
                <div className="quick-actions" role="group" aria-labelledby="quick-actions-title">
                  <button type="button" disabled={questionDisabled}
                    onClick={() => void analyze("내 계좌의 종합 리스크를 스캔해 줘. 보유 비중 상위 종목의 공시, 오버행, 수급을 함께 진단해 줘.", "종합 리스크 스캔")}>
                    <ShieldCheck size={18} /> 종합 리스크 스캔 <ArrowUpRight className="action-arrow" size={14} />
                  </button>
                  <button type="button" disabled={questionDisabled || !simulationHolding}
                    onClick={() => simulationHolding && void analyze(`${simulationHolding.stock_code} 종목을 주문 가능 현금의 40%로 추가매수하면 예상 평단가와 비중이 어떻게 바뀌는지 물타기 시뮬레이션해 줘.`, `${simulationHolding.name} · 추가매수 시뮬레이션`)}>
                    <Wallet size={18} /> 추가매수 시뮬레이션 <ArrowUpRight className="action-arrow" size={14} />
                  </button>
                  <button type="button" disabled={questionDisabled}
                    onClick={() => void analyze("최근 원/달러 환율과 금리 변화가 내 포트폴리오에 어떤 영향을 주는지 매크로 진단해 줘.", "환율·금리 영향")}>
                    <Globe2 size={18} /> 매크로 환율 진단 <ArrowUpRight className="action-arrow" size={14} />
                  </button>
                </div>
                {simulationHolding && (
                  <div className="simulation-options">
                    <label htmlFor="simulation-stock">추가매수 대상</label>
                    <select id="simulation-stock" value={simulationHolding.stock_code} disabled={questionDisabled}
                      onChange={(event) => setSimulationCode(event.target.value)}>
                      {holdingOptions.map((holding) => <option key={holding.stock_code} value={holding.stock_code}>{holding.name}</option>)}
                    </select>
                    <span>주문 가능 현금의 40%</span>
                  </div>
                )}
                {(running || elapsed !== null) && (
                  <AnalysisProgress statuses={Object.values(statuses)} holdings={holdingOptions} elapsed={elapsed} />
                )}
              </section>
              {topic && <section className="panel analysis-panel" aria-labelledby="report-title" aria-busy={running}>
                <div className="section-heading report-heading">
                  <h2 id="report-title"><FileText size={19} /> 진단 리포트</h2>
                  <p className="report-topic" title={topic}>{topic}</p>
                </div>
                {analysisError && <p className="error" role="alert">{analysisError}</p>}
                {report && <ReportBody report={report} />}
                {warnings.length > 0 && (
                  <div className="warning" role="alert">
                    <CircleAlert size={18} />
                    <div><strong>검증 참고사항</strong>
                      <ul>{warnings.map((warning, index) => <li key={index}>{warning}</li>)}</ul>
                    </div>
                  </div>
                )}
              </section>}
            </div>
          </div>
        </main>
      )}
    </>
  );
}
