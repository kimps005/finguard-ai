"use client";

import { useCallback, useEffect, useRef, useState, type FormEvent } from "react";
import {
  ArrowRight, Check, CircleAlert, Globe2, LoaderCircle, LockKeyhole, ShieldCheck, Wallet, X,
} from "lucide-react";
import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";
import {
  readChatEvents, type AccountSnapshot, type DecimalString, type Scorecards, type Status, type StockSearchResult,
} from "../lib/chat-stream";
import PriceOutlook, { WATCHLIST } from "./price-outlook";

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
  return [subject, ...keywords].filter(Boolean).join(", ") || "포트폴리오 진단";
}

function AnalysisProgress({ statuses, holdings, elapsed }: {
  statuses: Status[];
  holdings: AccountSnapshot["holdings"];
  elapsed: number | null;
}) {
  const steps = statuses.map((status) => ({ phase: status.phase, label: describeTask(status, holdings) }));
  const timeline = steps.length > 0 && (
    <ol className="timeline">
      {steps.map(({ phase, label }, index) => (
        <li key={`${label}-${index}`} data-phase={phase}>
          <span className="timeline-dot" aria-hidden="true">
            {phase === "completed" && <Check size={11} strokeWidth={3} />}
            {phase === "failed" && <X size={11} strokeWidth={3} />}
          </span>
          <span className="timeline-label">{label}</span>
          <span className="sr-only">{phase === "started" ? "진행 중" : phase === "failed" ? "실패" : "완료"}</span>
        </li>
      ))}
    </ol>
  );

  if (elapsed !== null) {
    const seconds = Math.max(1, Math.round(elapsed / 1000));
    const duration = seconds < 60 ? `${seconds}초` : `${Math.floor(seconds / 60)}분 ${seconds % 60}초`;
    return (
      <details className="progress progress-done">
        <summary>
          <span className="done-icon" aria-hidden="true"><Check size={12} strokeWidth={3} /></span>
          <span role="status">{duration} 만에 분석을 마쳤습니다</span>
          {steps.length > 0 && <span className="progress-toggle">단계 {steps.length}개 보기</span>}
        </summary>
        {timeline}
      </details>
    );
  }

  const current = statuses.filter((status) => status.phase === "started").at(-1) ?? statuses.at(-1);
  const title = current ? describeTask(current, holdings) : stageNames.router_node;
  return (
    <div className="progress progress-live">
      <p className="progress-title" role="status">
        <span className="pulse" aria-hidden="true" />
        {title}
      </p>
      {timeline}
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
  const [stock, setStock] = useState<StockSearchResult>(WATCHLIST[0]);
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
    setStock(WATCHLIST[0]);
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

  const quickActions = [
    {
      key: "risk", icon: ShieldCheck, title: "종합 리스크 점검", disabled: false,
      description: "비중 상위 종목의 공시·오버행·수급",
      run: () => analyze("내 계좌의 종합 리스크를 스캔해 줘. 보유 비중 상위 종목의 공시, 오버행, 수급을 함께 진단해 줘.", "종합 리스크 점검"),
    },
    {
      key: "simulation", icon: Wallet, title: "추가매수 시뮬레이션", disabled: !simulationHolding,
      description: simulationHolding ? `${simulationHolding.name}, 매수 가능 금액의 40%` : "보유 종목이 있어야 합니다",
      run: () => simulationHolding && analyze(`${simulationHolding.stock_code} 종목을 주문 가능 현금의 40%로 추가매수하면 예상 평단가와 비중이 어떻게 바뀌는지 물타기 시뮬레이션해 줘.`, `${simulationHolding.name} 추가매수 시뮬레이션`),
    },
    {
      key: "macro", icon: Globe2, title: "환율·금리 영향", disabled: false,
      description: "원/달러 환율과 금리가 계좌에 주는 영향",
      run: () => analyze("최근 원/달러 환율과 금리 변화가 내 포트폴리오에 어떤 영향을 주는지 매크로 진단해 줘.", "환율·금리 영향"),
    },
  ];
  const ownedCodes = new Set(holdingOptions.map(({ stock_code }) => stock_code));
  const watchlist = WATCHLIST.filter(({ stock_code }) => !ownedCodes.has(stock_code));

  return (
    <>
      <header className="topbar">
        <div className="shell topbar-inner">
          <span className="brand">
            <span className="brand-mark" aria-hidden="true"><ShieldCheck size={16} strokeWidth={2.4} /></span>
            FinGuard
          </span>
          {pin && <button type="button" className="ghost-button" onClick={() => lock()}>
            <LockKeyhole size={14} aria-hidden="true" /> 잠그기
          </button>}
        </div>
      </header>
      {!pin ? (
        <main className="lock-page">
          <form className="lock-card" onSubmit={submitPin}>
            <h1>대시보드 잠금 해제</h1>
            <p>계좌 정보를 보려면 접근 PIN을 입력하세요.</p>
            <label htmlFor="pin">접근 PIN</label>
            <input id="pin" type="password" value={pinInput} onChange={(event) => setPinInput(event.target.value)}
              autoComplete="off" maxLength={128} required autoFocus aria-describedby={accessError ? "pin-error" : undefined} />
            {accessError && <p id="pin-error" className="inline-error" role="alert">{accessError}</p>}
            <button className="primary-button" disabled={checking || !pinInput.trim()}>
              {checking ? <><LoaderCircle size={16} className="animate-spin motion-reduce:animate-none" aria-hidden="true" /> 확인 중</>
                : <>잠금 해제 <ArrowRight size={16} aria-hidden="true" /></>}
            </button>
          </form>
        </main>
      ) : (
        <main className="shell workspace">
          <aside className="rail" aria-label="계좌와 종목">
            <section className="account" aria-labelledby="account-title">
              <h2 id="account-title" className="sr-only">계좌 요약</h2>
              {summary ? (
                <dl>
                  <div className="account-total">
                    <dt>{summary.cash_deposit === null ? "국내 주식 평가액" : "총 평가액"}</dt>
                    <dd>
                      <span className="account-amount">{won.format(summary.total_value)}<small>원</small></span>
                      {accountChange && <span className="delta" data-direction={accountChange.direction}
                        aria-label={`평가손익률 ${accountChange.text}`}>{accountChange.text}</span>}
                    </dd>
                  </div>
                  {(summary.orderable_cash !== null || summary.cash_weight !== null) && (
                    <div className="account-facts">
                      {summary.orderable_cash !== null && (
                        <div><dt>매수 가능 금액</dt><dd>{won.format(summary.orderable_cash)}원</dd></div>
                      )}
                      {summary.cash_weight !== null && (
                        <div><dt>현금 비중</dt><dd>{percent.format(summary.cash_weight)}</dd></div>
                      )}
                    </div>
                  )}
                </dl>
              ) : accountError ? (
                <p className="inline-error" role="alert">{accountError}</p>
              ) : (
                <div className="account-loading" role="status">
                  <span className="skeleton skeleton-label" /><span className="skeleton skeleton-amount" />
                  <span className="sr-only">계좌 정보를 불러오는 중입니다.</span>
                </div>
              )}
            </section>

            {summary && (
              <section className="stock-list" aria-labelledby="holdings-title">
                <h2 id="holdings-title">보유 종목 <span>{holdingOptions.length}</span></h2>
                {holdingOptions.length === 0 ? <p className="stock-list-empty">보유 종목이 없습니다. 아래 관심 종목이나 검색으로 가격 구간을 확인하세요.</p> : (
                  <ul>
                    {holdingOptions.map((holding) => {
                      const change = holdingReturn(holding.profit_loss_pct);
                      const score = holding.asset_type === "STOCK" ? scores[holding.stock_code]?.total : null;
                      const selectable = /^[0-9A-Z]{6}$/.test(holding.stock_code);
                      const body = <>
                        <span className="stock-row-main">
                          <strong>{holding.name}</strong>
                          <span className="delta" data-direction={change.direction} aria-label={`평가손익률 ${change.text}`}>{change.text}</span>
                        </span>
                        <span className="stock-row-meta">
                          <span>{holding.stock_code}</span>
                          <span>평단 {won.format(holding.average_price)}원</span>
                          {score != null && <span className="score" data-tone={score >= 70 ? "good" : score >= 40 ? "mid" : "low"}>점수 {score}</span>}
                        </span>
                      </>;
                      return (
                        <li key={holding.stock_code}>
                          {selectable ? (
                            <button type="button" className="stock-row" aria-pressed={stock.stock_code === holding.stock_code}
                              onClick={() => setStock({ stock_code: holding.stock_code, corp_name: holding.name, market: null })}>
                              {body}
                            </button>
                          ) : <div className="stock-row">{body}</div>}
                        </li>
                      );
                    })}
                  </ul>
                )}
              </section>
            )}

            {watchlist.length > 0 && (
              <section className="stock-list" aria-labelledby="watchlist-title">
                <h2 id="watchlist-title">관심 종목</h2>
                <ul>
                  {watchlist.map((item) => (
                    <li key={item.stock_code}>
                      <button type="button" className="stock-row" aria-pressed={stock.stock_code === item.stock_code}
                        onClick={() => setStock(item)}>
                        <span className="stock-row-main"><strong>{item.corp_name}</strong></span>
                        <span className="stock-row-meta"><span>{item.stock_code}</span>{item.market && <span>{item.market}</span>}</span>
                      </button>
                    </li>
                  ))}
                </ul>
              </section>
            )}
          </aside>

          <div className="main-column">
            {summary && accountError && <p className="inline-note" role="status">{accountError}</p>}
            <PriceOutlook pin={pin} pinHeader={PIN_HEADER} stock={stock} onStockChange={setStock} onUnauthorized={lock} />

            <section className="sheet diagnosis" aria-labelledby="diagnosis-title" aria-busy={running}>
              <div className="block-head">
                <h2 id="diagnosis-title">AI 계좌 진단</h2>
                <p>보유 종목의 공시·재무·수급을 근거와 함께 분석합니다.</p>
              </div>
              <form className="ask" onSubmit={(event) => {
                event.preventDefault();
                if (questionDisabled || !query.trim()) return;
                void analyze(query);
                setQuery("");
              }}>
                <label className="sr-only" htmlFor="query">계좌 진단 질문</label>
                <input id="query" maxLength={4000} placeholder="예: 손실 중인 종목을 계속 들고 가도 될까?" value={query}
                  onChange={(event) => setQuery(event.target.value)} />
                <button className="primary-button" disabled={questionDisabled || !query.trim()}>
                  {running ? <LoaderCircle size={16} className="animate-spin motion-reduce:animate-none" aria-hidden="true" /> : null}
                  {running ? "분석 중" : "질문하기"}
                </button>
              </form>

              <div className="quick-actions" role="group" aria-label="자주 쓰는 진단">
                {quickActions.map(({ key, icon: Icon, title, description, disabled, run }) => (
                  <button key={key} type="button" disabled={questionDisabled || disabled} onClick={() => void run()}>
                    <Icon size={17} aria-hidden="true" />
                    <span><strong>{title}</strong><span>{description}</span></span>
                  </button>
                ))}
              </div>
              {holdingOptions.length > 1 && (
                <div className="simulation-target">
                  <label htmlFor="simulation-stock">시뮬레이션 종목</label>
                  <select id="simulation-stock" value={simulationHolding?.stock_code} disabled={questionDisabled}
                    onChange={(event) => setSimulationCode(event.target.value)}>
                    {holdingOptions.map((holding) => <option key={holding.stock_code} value={holding.stock_code}>{holding.name}</option>)}
                  </select>
                </div>
              )}

              {topic ? (
                <div className="report" aria-labelledby="report-title">
                  <div className="report-head">
                    <h2 id="report-title">{topic}</h2>
                  </div>
                  {(running || elapsed !== null) && (
                    <AnalysisProgress statuses={Object.values(statuses)} holdings={holdingOptions} elapsed={elapsed} />
                  )}
                  {analysisError && <p className="inline-error" role="alert">{analysisError}</p>}
                  {report && <ReportBody report={report} />}
                  {warnings.length > 0 && (
                    <div className="callout" role="note">
                      <CircleAlert size={17} aria-hidden="true" />
                      <div><strong>검증 참고사항</strong>
                        <ul>{warnings.map((warning, index) => <li key={index}>{warning}</li>)}</ul>
                      </div>
                    </div>
                  )}
                </div>
              ) : (
                <p className="diagnosis-empty">질문을 입력하거나 위 진단 중 하나를 고르면, 진행 단계와 리포트가 여기에 표시됩니다.</p>
              )}
            </section>
          </div>
        </main>
      )}
    </>
  );
}
