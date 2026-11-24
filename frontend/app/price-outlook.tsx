"use client";

import { useCallback, useEffect, useRef, useState, type FormEvent } from "react";
import { ExternalLink, RefreshCw, Search } from "lucide-react";
import TossTenMinuteChart from "./toss-ten-minute-chart";
import {
  SessionExpired, streamChat, type DecimalString, type StockPriceBand,
  type StockPriceBands, type StockSearchResult,
} from "../lib/chat-stream";

const CACHE_PREFIX = "finguard-stock-outlook-v1:";
export const WATCHLIST: StockSearchResult[] = [
  { stock_code: "005930", corp_name: "삼성전자", market: "KOSPI" },
  { stock_code: "000660", corp_name: "SK하이닉스", market: "KOSPI" },
  { stock_code: "005380", corp_name: "현대차", market: "KOSPI" },
];
const won = new Intl.NumberFormat("ko-KR", { maximumFractionDigits: 2 });
const analyzedAt = new Intl.DateTimeFormat("ko-KR", {
  dateStyle: "medium", timeStyle: "short", timeZone: "Asia/Seoul",
});

export function direction(value: DecimalString | null) {
  if (value === null || !/[1-9]/.test(value)) return "flat";
  return value.startsWith("-") ? "loss" : "gain";
}

export function signedPercent(value: DecimalString | null) {
  if (value === null) return "자료 없음";
  const nonzero = /[1-9]/.test(value);
  return nonzero ? `${value.startsWith("-") ? "" : "+"}${won.format(value)}%` : "0%";
}

function readCache(code: string): StockPriceBands | null {
  try {
    const cached = JSON.parse(sessionStorage.getItem(CACHE_PREFIX + code) ?? "null");
    return cached?.band?.stock_code === code && Number.isFinite(Date.parse(cached.as_of))
      && cached.band.levels ? cached : null;
  } catch {
    return null;
  }
}

function PriceLadder({ band }: { band: StockPriceBand }) {
  const { buy_zone: buy, sell_zone: sell, peak_limit: ceiling } = band.levels;
  // Number() only positions marks on screen; every displayed value stays the backend's decimal string.
  const rows = [
    { key: "ceiling", label: "1개월 상단", hint: "단기적으로 넘기 어려운 가격", price: ceiling.price, pct: ceiling.current_pct, reason: ceiling.reason },
    { key: "sell", label: "매도 추천가", hint: "이익 실현을 고려할 가격", price: sell.price, pct: sell.current_pct, reason: sell.reason },
    { key: "current", label: "현재가", hint: "분석 시점", price: band.current_price, pct: null, reason: "" },
    { key: "buy", label: "매수 추천가", hint: "분할 매수를 고려할 가격", price: buy.price, pct: buy.current_pct, reason: buy.reason },
  ].sort((left, right) => Number(right.price) - Number(left.price));
  const values = rows.map(({ price }) => Number(price));
  const low = Math.min(...values);
  const span = Math.max(...values) - low || 1;
  const position = (price: DecimalString) => `${((Number(price) - low) / span) * 100}%`;

  return (
    <div className="ladder">
      <div className="ladder-range" aria-hidden="true">
        <span className="ladder-zone" style={{ left: position(buy.price), right: `calc(100% - ${position(sell.price)})` }} />
        {rows.map(({ key, price }) => <span key={key} className="ladder-mark" data-level={key} style={{ left: position(price) }} />)}
      </div>
      <ol className="ladder-rows">
        {rows.map(({ key, label, hint, price, pct, reason }) => (
          <li key={key} data-level={key}>
            <div className="ladder-label">
              <strong>{label}</strong>
              <span>{hint}</span>
            </div>
            <div className="ladder-price">
              <strong>{won.format(price)}<small>원</small></strong>
              {pct !== null && <span data-direction={direction(pct)}>현재가 대비 {signedPercent(pct)}</span>}
            </div>
            {reason && <p className="ladder-reason">{reason}</p>}
          </li>
        ))}
      </ol>
    </div>
  );
}

function Evidence({ band }: { band: StockPriceBand }) {
  if (band.ma20 === null && !band.recent_range && !band.flow && band.disclosures.length === 0) return null;
  return (
    <section className="evidence" aria-labelledby="evidence-title">
      <h3 id="evidence-title">판단 근거</h3>
      <dl>
        {band.ma20 !== null && <div><dt>20일 이동평균</dt><dd>{won.format(band.ma20)}원</dd></div>}
        {band.recent_range && <div>
          <dt>최근 {band.recent_range.sessions}거래일 범위</dt>
          <dd>{won.format(band.recent_range.low)}원 ~ {won.format(band.recent_range.high)}원</dd>
        </div>}
        {band.flow && <>
          <div><dt>외국인 5일 순매수</dt><dd data-direction={direction(band.flow.foreign)}>{won.format(band.flow.foreign)}주</dd></div>
          <div><dt>기관 5일 순매수</dt><dd data-direction={direction(band.flow.institution)}>{won.format(band.flow.institution)}주</dd></div>
        </>}
      </dl>
      {band.disclosures.length > 0 && (
        <ul className="evidence-docs" aria-label="DART 공시">
          {band.disclosures.map((doc) => (
            <li key={doc.alias}>
              <a href={`https://dart.fss.or.kr/dsaf001/main.do?rcpNo=${encodeURIComponent(doc.receipt)}`}
                target="_blank" rel="noreferrer">
                <span className="doc-alias">{doc.alias}</span>
                <span className="doc-title">{doc.title}</span>
                <ExternalLink size={14} aria-label="새 창에서 DART 공시 열기" />
              </a>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

export default function PriceOutlook({ pin, pinHeader, stock, onStockChange, onUnauthorized }: {
  pin: string;
  pinHeader: string;
  stock: StockSearchResult;
  onStockChange: (stock: StockSearchResult) => void;
  onUnauthorized: (message: string) => void;
}) {
  const [query, setQuery] = useState("");
  const [open, setOpen] = useState(false);
  const [matches, setMatches] = useState<{ query: string; stocks: StockSearchResult[] }>();
  const [highlighted, setHighlighted] = useState(-1);
  const [searchError, setSearchError] = useState("");
  const [result, setResult] = useState<StockPriceBands | null>(null);
  const [running, setRunning] = useState(true);
  const [error, setError] = useState("");
  const activeCode = useRef(stock.stock_code);
  const requests = useRef(new Map<string, AbortController>());
  const snapshots = useRef(new Map<string, StockPriceBands>());
  const submitRequest = useRef<AbortController | null>(null);
  const composing = useRef(false);

  const analyze = useCallback(async (target: StockSearchResult, force = false) => {
    const code = target.stock_code;
    activeCode.current = code;
    setError("");
    const cached = snapshots.current.get(code) ?? readCache(code);
    setResult(cached);
    if (requests.current.has(code)) {
      setRunning(true);
      return;
    }
    if (cached && !force) {
      setRunning(false);
      return;
    }

    const controller = new AbortController();
    requests.current.set(code, controller);
    setRunning(true);

    function show(snapshot: StockPriceBands) {
      if (snapshot.band.stock_code !== code) return;
      const entry = { as_of: snapshot.as_of, band: snapshot.band };
      snapshots.current.set(code, entry);
      if (activeCode.current === code) setResult(entry);
      try {
        sessionStorage.setItem(CACHE_PREFIX + code, JSON.stringify(entry));
      } catch {
        // The in-memory cache also covers blocked browser storage.
      }
    }

    try {
      const events = streamChat({ mode: "price_bands", target_stock_code: code },
                                { pin, pinHeader, signal: controller.signal }, "가격 구간 분석을 시작할 수 없습니다.");
      for await (const item of events) {
        if (controller.signal.aborted) return;
        if (item.event === "price_bands" && "band" in item.data) show(item.data);
        if (item.event === "error") throw new Error(item.data.message);
        if (item.event === "done") {
          if (!item.data.ok || !("band" in item.data)) throw new Error("가격 구간 분석을 완료할 수 없습니다.");
          show(item.data);
          return;
        }
      }
      throw new Error("가격 구간 분석 연결이 끊겼습니다. 다시 분석을 눌러 주세요.");
    } catch (problem) {
      if (problem instanceof SessionExpired) {
        onUnauthorized(problem.message);
      } else if (!controller.signal.aborted && activeCode.current === code) {
        setError(problem instanceof Error ? problem.message : "가격 구간 분석을 완료할 수 없습니다.");
      }
    } finally {
      if (requests.current.get(code) === controller) {
        requests.current.delete(code);
        if (activeCode.current === code) setRunning(false);
      }
    }
  }, [onUnauthorized, pin, pinHeader]);

  useEffect(() => {
    const launch = window.setTimeout(() => void analyze(stock), 0);
    return () => window.clearTimeout(launch);
  }, [analyze, stock]);

  useEffect(() => {
    const pending = requests.current;
    return () => {
      pending.forEach((controller) => controller.abort());
      pending.clear();
      submitRequest.current?.abort();
    };
  }, []);

  const findStocks = useCallback(async (value: string, signal: AbortSignal): Promise<StockSearchResult[]> => {
    const response = await fetch("/api/stocks/search?" + new URLSearchParams({ q: value }), {
      headers: { [pinHeader]: pin }, signal,
    });
    if (response.status === 401) {
      onUnauthorized("접근이 만료되었습니다. PIN을 다시 입력해 주세요.");
      return [];
    }
    if (!response.ok) throw new Error("종목 검색을 완료할 수 없습니다.");
    return response.json();
  }, [onUnauthorized, pin, pinHeader]);

  const term = query.trim();
  const suggestions = open && matches?.query === term ? matches.stocks : undefined;
  useEffect(() => {
    if (!open || !term) return;
    const controller = new AbortController();
    const timer = window.setTimeout(async () => {
      try {
        const stocks = await findStocks(term, controller.signal);
        if (!controller.signal.aborted) setMatches({ query: term, stocks });
      } catch (problem) {
        if (!controller.signal.aborted) setSearchError(
          problem instanceof Error ? problem.message : "종목 검색을 완료할 수 없습니다.");
      }
    }, 180);
    return () => {
      window.clearTimeout(timer);
      controller.abort();
    };
  }, [findStocks, open, term]);

  function selectStock(next: StockSearchResult) {
    submitRequest.current?.abort();
    setOpen(false);
    setHighlighted(-1);
    setQuery("");
    setSearchError("");
    onStockChange(next);
  }

  async function submitSearch(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!term || composing.current) return;
    const suggested = suggestions?.[highlighted] ?? suggestions?.[0];
    if (suggested) {
      selectStock(suggested);
      return;
    }

    submitRequest.current?.abort();
    const controller = new AbortController();
    submitRequest.current = controller;
    setOpen(false);
    setSearchError("");
    try {
      const stocks = suggestions ?? await findStocks(term, controller.signal);
      if (controller.signal.aborted) return;
      if (stocks[0]) selectStock(stocks[0]);
      else setSearchError(`'${term}'과 일치하는 종목이 없습니다. 종목명이나 6자리 코드를 확인해 주세요.`);
    } catch (problem) {
      if (!controller.signal.aborted) setSearchError(
        problem instanceof Error ? problem.message : "종목 검색을 완료할 수 없습니다.");
    } finally {
      if (submitRequest.current === controller) submitRequest.current = null;
    }
  }

  const band = result?.band;
  return (
    <section className="sheet outlook" aria-labelledby="price-outlook-title" aria-busy={running}>
      <form className="stock-search" role="search" onSubmit={(event) => void submitSearch(event)}
        onBlur={(event) => {
          if (!event.currentTarget.contains(event.relatedTarget)) setOpen(false);
        }}>
        <Search size={18} aria-hidden="true" />
        <label className="sr-only" htmlFor="stock-search">종목 검색</label>
        <input id="stock-search" value={query} maxLength={100} autoComplete="off"
          role="combobox" aria-autocomplete="list" aria-expanded={Boolean(suggestions?.length)}
          aria-controls="stock-suggestions"
          aria-activedescendant={suggestions?.[highlighted] ? `stock-option-${highlighted}` : undefined}
          placeholder="다른 종목 찾기: 종목명 또는 6자리 코드"
          onFocus={() => { setOpen(true); setHighlighted(-1); }}
          onCompositionStart={() => { composing.current = true; }}
          onCompositionEnd={() => { composing.current = false; }}
          onChange={(event) => {
            submitRequest.current?.abort();
            setQuery(event.target.value);
            setOpen(true);
            setHighlighted(-1);
            setSearchError("");
          }}
          onKeyDown={(event) => {
            if (event.nativeEvent.isComposing || composing.current) return;
            if (event.key === "Escape") {
              setOpen(false);
              setHighlighted(-1);
            }
            if (suggestions?.length && (event.key === "ArrowDown" || event.key === "ArrowUp")) {
              event.preventDefault();
              const step = event.key === "ArrowDown" ? 1 : -1;
              setHighlighted((index) => index < 0 && step === -1 ? suggestions.length - 1
                : (index + step + suggestions.length) % suggestions.length);
            }
          }} />
        <button type="submit" className="search-submit" disabled={!term}>검색</button>
        <ul id="stock-suggestions" role="listbox" aria-label="검색 결과" className="stock-suggestions"
          hidden={!suggestions?.length}>
          {suggestions?.map((item, index) => (
            <li key={item.stock_code} role="presentation">
              <button type="button" role="option" id={`stock-option-${index}`} aria-selected={index === highlighted}
                tabIndex={-1} onMouseDown={(event) => event.preventDefault()} onClick={() => selectStock(item)}>
                <strong>{item.corp_name}</strong>
                <span>{item.stock_code}{item.market && <em>{item.market}</em>}</span>
              </button>
            </li>
          ))}
        </ul>
      </form>
      {searchError && <p className="inline-error" role="alert">{searchError}</p>}
      {suggestions?.length === 0 && !searchError && <p className="inline-note" role="status">일치하는 종목이 없습니다.</p>}

      <header className="outlook-head">
        <div className="min-w-0">
          <h1 id="price-outlook-title">
            {band?.name ?? stock.corp_name}
            <span className="code">{stock.stock_code}</span>
          </h1>
          <p className="outlook-sub">
            {band?.change_pct != null && (
              <span className="delta" data-direction={direction(band.change_pct)}>전일 대비 {signedPercent(band.change_pct)}</span>
            )}
            {result && <time dateTime={result.as_of}>{analyzedAt.format(new Date(result.as_of))} 분석</time>}
          </p>
        </div>
        <button type="button" className="ghost-button" disabled={running} onClick={() => void analyze(stock, true)}>
          <RefreshCw size={15} aria-hidden="true" className={running ? "animate-spin motion-reduce:animate-none" : undefined} />
          {running ? "분석 중" : "다시 분석"}
        </button>
      </header>

      <TossTenMinuteChart key={stock.stock_code} stockCode={stock.stock_code} band={band}
        pin={pin} pinHeader={pinHeader} onUnauthorized={onUnauthorized} />

      <div className="outlook-body">
        <section aria-labelledby="ladder-title">
          <div className="block-head">
            <h2 id="ladder-title">1개월 가격 구간</h2>
            {band?.indicative && <p>변동폭 자료가 부족해 현재가 기준 참고 가격입니다.</p>}
          </div>
          {band ? <PriceLadder band={band} /> : (
            <div className="ladder-placeholder" role="status">
              {error ? <p className="inline-error">{error}</p> : <p>{stock.corp_name}의 매수·매도 가격 구간을 계산하고 있습니다.</p>}
            </div>
          )}
          {band && error && <p className="inline-error" role="alert">{error}</p>}
        </section>
        {band && <Evidence band={band} />}
      </div>
    </section>
  );
}
