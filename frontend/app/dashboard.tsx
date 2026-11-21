"use client";

import { useEffect, useRef, useState, type FormEvent } from "react";
import {
  Activity, ArrowRight, ArrowUpRight, Check, CircleAlert, FileText,
  Globe2, Layers3, LoaderCircle, LockKeyhole, Send, ShieldCheck, Wallet,
} from "lucide-react";
import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { readChatEvents, type AccountSnapshot, type DecimalString, type Scorecards, type Status } from "../lib/chat-stream";

const PIN_KEY = "finguard-app-pin";
const PIN_HEADER = "X-App-Pin";
const stages = [
  { nodes: ["router_node"], name: "Router", detail: "질문 분류" },
  { nodes: ["fast_quote_node", "market_data_node"], name: "Fast Quote / Market ETL", detail: "잔고·시장 데이터" },
  { nodes: ["quant_scorecard_node"], name: "Quant Scorecard", detail: "정량 점수 산출" },
  { nodes: ["stock_worker"], name: "Top-N Stock Workers", detail: "보유 종목 병렬 분석" },
  { nodes: ["guardrail_node"], name: "Guardrail", detail: "근거 및 인용 검증" },
  { nodes: ["synthesizer_node"], name: "Synthesizer", detail: "리포트 작성" },
];

const won = new Intl.NumberFormat("ko-KR", { maximumFractionDigits: 2 });
const percent = new Intl.NumberFormat("ko-KR", { style: "percent", maximumFractionDigits: 2 });

function holdingReturn(value: DecimalString) {
  const negative = value.startsWith("-");
  const nonzero = /[1-9]/.test(value);
  return {
    color: nonzero ? (negative ? "text-sky-700" : "text-rose-700") : "text-slate-600",
    text: `${nonzero && !negative ? "+" : ""}${won.format(value)}%`,
  };
}

function Timeline({ statuses, complete }: { statuses: Status[]; complete: boolean }) {
  return (
    <ol className="timeline">
      {stages.map((stage) => {
        const tasks = statuses.filter((status) => stage.nodes.includes(status.node));
        const active = tasks.some((task) => task.phase === "started");
        const failed = tasks.some((task) => task.phase === "failed");
        const done = tasks.filter((task) => task.phase === "completed").length;
        const phase = failed ? "failed" : active ? "active" : done ? "done" : "waiting";
        const detail = tasks[0]?.node === "fast_quote_node" ? "경량 잔고 조회"
          : tasks[0]?.node === "market_data_node" ? "시장 데이터 수집"
          : tasks.length > 1 ? `${tasks.length}개 중 ${done}개 완료` : stage.detail;
        const label = { failed: "실패", active: "진행 중", done: "완료", waiting: complete ? "건너뜀" : "대기" }[phase];
        return (
          <li key={stage.name} className="timeline-step" data-phase={phase}>
            <span className="step-icon" aria-hidden="true">
              {phase === "done" && <Check size={13} />}
              {phase === "active" && <LoaderCircle size={13} className="animate-spin motion-reduce:animate-none" />}
              {phase === "failed" && <CircleAlert size={13} />}
            </span>
            <span className="step-name">{stage.name}</span>
            <span className="step-detail">{phase === "waiting" && complete ? "건너뜀" : detail}</span>
            <span className="sr-only">{label}</span>
          </li>
        );
      })}
    </ol>
  );
}

export default function Dashboard() {
  const [pin, setPin] = useState("");
  const [pinInput, setPinInput] = useState("");
  const [checking, setChecking] = useState(true);
  const [accessError, setAccessError] = useState("");
  const [account, setAccount] = useState<AccountSnapshot | null>(null);
  const [scores, setScores] = useState<Scorecards>({});
  const [simulationCode, setSimulationCode] = useState("");
  const [query, setQuery] = useState("");
  const [lastQuery, setLastQuery] = useState("");
  const [running, setRunning] = useState(false);
  const [complete, setComplete] = useState(false);
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

  function lock(message = "") {
    requestRef.current?.abort();
    requestRef.current = null;
    sessionStorage.removeItem(PIN_KEY);
    setPin("");
    setPinInput("");
    setAccount(null);
    setScores({});
    setSimulationCode("");
    setQuery("");
    setLastQuery("");
    setReport("");
    setStatuses({});
    setWarnings([]);
    setAnalysisError("");
    setAccessError(message);
    setChecking(false);
    setRunning(false);
    setComplete(false);
  }

  async function unlock(candidate: string, signal: AbortSignal) {
    setChecking(true);
    setAccessError("");
    try {
      const response = await fetch("/api/account", { headers: { [PIN_HEADER]: candidate }, signal });
      if (response.status === 401) {
        lock("접근 PIN을 확인해 주세요.");
        return;
      }
      if (!response.ok) throw new Error("계좌 정보를 불러오지 못했습니다. 잠시 후 다시 시도해 주세요.");
      const snapshot: AccountSnapshot = await response.json();
      sessionStorage.setItem(PIN_KEY, candidate);
      setAccount(snapshot);
      setPin(candidate);
      setPinInput("");
    } catch (error) {
      if (!signal.aborted) {
        setAccessError(error instanceof Error ? error.message : "계좌 서버에 연결할 수 없습니다.");
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

  async function analyze(value: string) {
    const question = value.trim();
    if (!pin || !question || requestRef.current) return;
    const controller = new AbortController();
    requestRef.current = controller;
    setLastQuery(question);
    setRunning(true);
    setComplete(false);
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
          setStatuses((current) => ({ ...current, [key]: status }));
        } else if (item.event === "token") {
          setReport((current) => current + item.data.text);
        } else if (item.event === "error") {
          throw new Error(item.data.message);
        } else {
          const result = item.data;
          if (!result.ok) throw new Error("계좌 분석을 완료할 수 없습니다.");
          setReport(result.final_report);
          setWarnings(result.guardrail_warnings);
          setAccount((current) => ({
            holdings: result.holdings,
            account_summary: {
              ...result.account_summary,
              orderable_cash: current?.account_summary.orderable_cash ?? null,
            },
          }));
          if (result.intent_type !== "fast_lookup") setScores(result.quant_scorecards);
          setComplete(true);
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
  const holdingOptions = account?.holdings ?? [];
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
      {!pin || !account || !summary ? (
        <main className="lock-page">
          <form className="panel lock-panel" onSubmit={submitPin}>
            <span className="section-icon"><LockKeyhole size={23} /></span>
            <h1>대시보드 잠금 해제</h1>
            <p className="muted">계좌를 조회하려면 접근 PIN을 입력하세요.</p>
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
          <div className="page-heading">
            <h1>포트폴리오 대시보드</h1>
            <p>국내 주식 계좌와 보유 종목의 리스크를 확인하세요.</p>
          </div>
          <div className="dashboard-grid">
            <aside className="min-w-0 space-y-5" aria-label="계좌와 보유 종목">
              <section className="panel account-panel" aria-labelledby="account-title">
                <h2 id="account-title"><Wallet size={17} /> 계좌 요약</h2>
                <dl>
                  <div className="account-total"><dt>총 평가액</dt><dd>{won.format(summary.total_value)}<small>원</small></dd></div>
                  <div className="account-cash">
                    <div>
                      <dt>주문 가능 예수금</dt>
                      <dd>{summary.orderable_cash === null ? "조회 불가" : `${won.format(summary.orderable_cash)}원`}</dd>
                      {summary.orderable_cash !== null && <span className="cash-reference">접속 시 조회</span>}
                    </div>
                    <div><dt>현금 비중</dt><dd>{percent.format(summary.cash_weight)}</dd></div>
                  </div>
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
                      const score = holding.asset_type !== "STOCK" ? "—"
                        : card?.total != null ? `${card.total}/100`
                        : card ? "자료 부족" : "진단 전";
                      return (
                        <li key={holding.stock_code} className="holding">
                          <div className="holding-heading">
                            <div className="min-w-0">
                              <h3>{holding.name}</h3>
                              <span className="ticker">{holding.stock_code}</span>
                            </div>
                            <span className="asset-badge" data-asset={holding.asset_type}>{holding.asset_type ?? "미분류"}</span>
                          </div>
                          <dl className="holding-metrics">
                            <div><dt>평단가</dt><dd>{won.format(holding.average_price)}원</dd></div>
                            <div><dt>평가손익률</dt><dd className={change.color}>{change.text}</dd></div>
                            <div><dt>정량 점수</dt><dd>{score}</dd></div>
                          </dl>
                        </li>
                      );
                    })}
                  </ul>
                )}
              </section>
            </aside>
            <div className="min-w-0 space-y-5">
              <section className="panel analysis-panel" aria-labelledby="diagnosis-title">
                <div className="section-heading"><h2 id="diagnosis-title"><Activity size={19} /> 빠른 진단</h2></div>
                <div className="quick-actions">
                  <button type="button" disabled={running}
                    onClick={() => void analyze("내 계좌의 종합 리스크를 스캔해 줘. 보유 비중 상위 종목의 공시, 오버행, 수급을 함께 진단해 줘.")}>
                    <ShieldCheck size={18} /> 종합 리스크 스캔 <ArrowUpRight className="action-arrow" size={14} />
                  </button>
                  <button type="button" disabled={running || !simulationHolding}
                    onClick={() => simulationHolding && void analyze(`${simulationHolding.stock_code} 종목을 주문 가능 현금의 40%로 추가매수하면 예상 평단가와 비중이 어떻게 바뀌는지 물타기 시뮬레이션해 줘.`)}>
                    <Wallet size={18} /> 예수금 물타기 시뮬레이션 <ArrowUpRight className="action-arrow" size={14} />
                  </button>
                  <button type="button" disabled={running}
                    onClick={() => void analyze("최근 원/달러 환율과 금리 변화가 내 포트폴리오에 어떤 영향을 주는지 매크로 진단해 줘.")}>
                    <Globe2 size={18} /> 매크로 환율 진단 <ArrowUpRight className="action-arrow" size={14} />
                  </button>
                </div>
                {simulationHolding && (
                  <div className="simulation-options">
                    <label htmlFor="simulation-stock">물타기 대상</label>
                    <select id="simulation-stock" value={simulationHolding.stock_code} disabled={running}
                      onChange={(event) => setSimulationCode(event.target.value)}>
                      {holdingOptions.map((holding) => <option key={holding.stock_code} value={holding.stock_code}>{holding.name}</option>)}
                    </select>
                    <span>주문 가능 현금의 40%</span>
                  </div>
                )}
                <form className="query-form" onSubmit={(event) => {
                  event.preventDefault();
                  if (running || !query.trim()) return;
                  void analyze(query);
                  setQuery("");
                }}>
                  <label className="sr-only" htmlFor="query">계좌 진단 질문</label>
                  <input id="query" maxLength={4000} placeholder="예: 내 계좌의 현재 잔고와 수익률은?" value={query}
                    onChange={(event) => setQuery(event.target.value)} />
                  <button className="primary-button" aria-label="질문 보내기" disabled={running || !query.trim()}><Send size={17} /></button>
                </form>
              </section>
              <section className="panel analysis-panel" aria-labelledby="timeline-title">
                <div className="section-heading">
                  <h2 id="timeline-title"><Activity size={19} /> 에이전트 타임라인</h2>
                  <span className="run-badge" data-active={running} role="status">
                    {running ? "분석 중" : complete ? "분석 완료" : analysisError ? "분석 중단" : "대기 중"}
                  </span>
                </div>
                <Timeline statuses={Object.values(statuses)} complete={complete} />
              </section>
              <section className="panel analysis-panel" aria-labelledby="report-title" aria-busy={running}>
                <div className="section-heading"><h2 id="report-title"><FileText size={19} /> 진단 리포트</h2></div>
                {warnings.length > 0 && (
                  <div className="warning" role="alert">
                    <CircleAlert size={18} />
                    <div><strong>검증을 통과하지 못한 분석을 제외했습니다.</strong>
                      <ul>{warnings.map((warning, index) => <li key={index}>{warning}</li>)}</ul>
                    </div>
                  </div>
                )}
                {lastQuery && <p className="report-query">{lastQuery}</p>}
                {analysisError && <p className="error" role="alert">{analysisError}</p>}
                {report ? (
                  <div className="markdown">
                    <Markdown remarkPlugins={[remarkGfm]} disallowedElements={["img"]}
                      components={{ table: ({ children }) => <div className="table-scroll"><table>{children}</table></div> }}>
                      {report}
                    </Markdown>
                  </div>
                ) : !analysisError && <p className="report-empty muted">{running ? "리포트를 작성하고 있습니다." : "진단을 시작하면 이곳에 결과가 표시됩니다."}</p>}
              </section>
            </div>
          </div>
        </main>
      )}
    </>
  );
}
