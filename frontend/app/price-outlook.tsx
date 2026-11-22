"use client";

import { useCallback, useEffect, useRef, useState, type FormEvent } from "react";
import { RefreshCw, Search } from "lucide-react";
import TossTenMinuteChart from "./toss-ten-minute-chart";
import {
  readChatEvents, type DecimalString, type Holding, type StockPriceBand,
  type StockPriceBands, type StockSearchResult,
} from "../lib/chat-stream";

const CACHE_PREFIX = "finguard-stock-outlook-v1:";
const WATCHLIST: StockSearchResult[] = [
  { stock_code: "005930", corp_name: "삼성전자", market: "KOSPI" },
  { stock_code: "000660", corp_name: "SK하이닉스", market: "KOSPI" },
  { stock_code: "005380", corp_name: "현대차", market: "KOSPI" },
];
const won = new Intl.NumberFormat("ko-KR", { maximumFractionDigits: 2 });
const analyzedAt = new Intl.DateTimeFormat("ko-KR", {
  dateStyle: "medium", timeStyle: "short", timeZone: "Asia/Seoul",
});

function signedPercent(value: DecimalString | null) {
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

function BandCards({ band }: { band: StockPriceBand }) {
  const { buy_zone: buy, sell_zone: sell, peak_limit: ceiling } = band.levels;
  const cards = [
    { key: "buy", label: "단기 예상 저점", action: "추천 매수가", level: buy,
      instruction: "여기서 사라", metric: "현재가 대비" },
    { key: "sell", label: "단기 예상 고점", action: "추천 매도가", level: sell,
      instruction: "여기서 팔아라", metric: "현재가 기준 기대수익률" },
    { key: "ceiling", label: "1개월 상단 한계", action: "단기적 고점", level: ceiling,
      instruction: "단기적으로 여기가 고점이다", metric: "현재가 대비" },
  ];
  return (
    <>
      {band.indicative && <p className="outlook-note">최근 변동폭 자료가 부족해 현재가 기준 참고 가격을 표시합니다.</p>}
      <div className="outlook-cards">
        {cards.map(({ key, label, action, instruction, level, metric }) => (
          <article key={key} className="outlook-card rounded-2xl bg-white tabular-nums" data-level={key}>
            <p className="outlook-card-label">{label}</p>
            <h3>{action}</h3>
            <p className="outlook-instruction">{instruction}</p>
            <p className="outlook-price">{won.format(level.price)}<span>원</span></p>
            <p className="outlook-returns">{metric} {signedPercent(level.current_pct)}</p>
            <p className="outlook-reason">{level.reason}</p>
          </article>
        ))}
      </div>
      {(band.ma20 !== null || band.recent_range || band.flow || band.disclosures.length > 0) && (
        <section className="outlook-evidence rounded-2xl bg-white" aria-label="핵심 근거">
          <h3>핵심 근거</h3>
          <ul>
            {band.ma20 !== null && <li>20일 이동평균선 {won.format(band.ma20)}원</li>}
            {band.recent_range && <li>최근 {band.recent_range.sessions}거래일 저가·고가 {won.format(band.recent_range.low)}원~{won.format(band.recent_range.high)}원</li>}
            {band.flow && <li>최근 5거래일 순매수 · 외국인 {won.format(band.flow.foreign)}주 · 기관 {won.format(band.flow.institution)}주</li>}
            {band.disclosures.length > 0 && <li>DART 공시
              {band.disclosures.map((doc) => (
                <a key={doc.alias} href={`https://dart.fss.or.kr/dsaf001/main.do?rcpNo=${encodeURIComponent(doc.receipt)}`}
                  target="_blank" rel="noreferrer">[{doc.alias}] {doc.title}</a>
              ))}
            </li>}
          </ul>
        </section>
      )}
    </>
  );
}

export default function PriceOutlook({ pin, pinHeader, holdings, onUnauthorized }: {
  pin: string;
  pinHeader: string;
  holdings: Holding[];
  onUnauthorized: (message: string) => void;
}) {
  const [selected, setSelected] = useState<StockSearchResult>(WATCHLIST[0]);
  const [query, setQuery] = useState("");
  const [open, setOpen] = useState(false);
  const [matches, setMatches] = useState<{ query: string; stocks: StockSearchResult[] }>();
  const [highlighted, setHighlighted] = useState(-1);
  const [searchError, setSearchError] = useState("");
  const [result, setResult] = useState<StockPriceBands | null>(null);
  const [running, setRunning] = useState(true);
  const [error, setError] = useState("");
  const activeCode = useRef(WATCHLIST[0].stock_code);
  const requests = useRef(new Map<string, AbortController>());
  const snapshots = useRef(new Map<string, StockPriceBands>());
  const submitRequest = useRef<AbortController | null>(null);
  const composing = useRef(false);

  const analyze = useCallback(async (stock: StockSearchResult, force = false) => {
    const code = stock.stock_code;
    activeCode.current = code;
    setSelected(stock);
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
      const response = await fetch("/api/chat", {
        method: "POST",
        headers: { [pinHeader]: pin, "Content-Type": "application/json" },
        body: JSON.stringify({ mode: "price_bands", target_stock_code: code }),
        signal: controller.signal,
      });
      if (response.status === 401) {
        onUnauthorized("접근이 만료되었습니다. PIN을 다시 입력해 주세요.");
        return;
      }
      if (!response.ok || !response.body) throw new Error("타점 분석을 시작할 수 없습니다.");
      for await (const item of readChatEvents(response.body)) {
        if (controller.signal.aborted) return;
        if (item.event === "price_bands" && "band" in item.data) show(item.data);
        if (item.event === "error") throw new Error(item.data.message);
        if (item.event === "done") {
          if (!item.data.ok || !("band" in item.data)) throw new Error("타점 분석을 완료할 수 없습니다.");
          show(item.data);
          return;
        }
      }
      throw new Error("타점 분석 연결이 끊겼습니다.");
    } catch (problem) {
      if (!controller.signal.aborted && activeCode.current === code) {
        setError(problem instanceof Error ? problem.message : "타점 분석을 완료할 수 없습니다.");
      }
    } finally {
      if (requests.current.get(code) === controller) {
        requests.current.delete(code);
        if (activeCode.current === code) setRunning(false);
      }
    }
  }, [onUnauthorized, pin, pinHeader]);

  useEffect(() => {
    const pending = requests.current;
    const launch = window.setTimeout(() => void analyze(WATCHLIST[0]), 0);
    return () => {
      window.clearTimeout(launch);
      pending.forEach((controller) => controller.abort());
      pending.clear();
      submitRequest.current?.abort();
    };
  }, [analyze]);

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

  function selectStock(stock: StockSearchResult) {
    submitRequest.current?.abort();
    setOpen(false);
    setHighlighted(-1);
    setQuery(stock.corp_name);
    setSearchError("");
    void analyze(stock);
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
      else setSearchError("일치하는 종목이 없습니다.");
    } catch (problem) {
      if (!controller.signal.aborted) setSearchError(
        problem instanceof Error ? problem.message : "종목 검색을 완료할 수 없습니다.");
    } finally {
      if (submitRequest.current === controller) submitRequest.current = null;
    }
  }

  const band = result?.band;
  const owned = holdings.filter(({ stock_code }) => /^[0-9A-Z]{6}$/.test(stock_code));
  const ownedCodes = new Set(owned.map(({ stock_code }) => stock_code));
  const watchlist = WATCHLIST.filter(({ stock_code }) => !ownedCodes.has(stock_code));
  return (
    <section className="price-outlook" aria-labelledby="price-outlook-title">
      <form className="stock-search" onSubmit={(event) => void submitSearch(event)}
        onBlur={(event) => {
          if (!event.currentTarget.contains(event.relatedTarget)) setOpen(false);
        }}>
        <Search size={22} aria-hidden="true" />
        <label className="sr-only" htmlFor="stock-search">종목 검색</label>
        <input id="stock-search" value={query} maxLength={100} autoComplete="off"
          role="combobox" aria-autocomplete="list" aria-expanded={Boolean(suggestions?.length)}
          aria-controls="stock-suggestions"
          aria-activedescendant={suggestions?.[highlighted] ? `stock-option-${highlighted}` : undefined}
          placeholder="종목명 또는 종목코드 검색, 예: 삼성전자, 한온시스템"
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
        <button type="submit" aria-label="종목 분석 시작">검색</button>
        <ul id="stock-suggestions" role="listbox" aria-label="검색 결과" className="stock-suggestions"
          hidden={!suggestions?.length}>
          {suggestions?.map((stock, index) => (
            <li key={stock.stock_code} role="presentation">
              <button type="button" role="option" id={`stock-option-${index}`} aria-selected={index === highlighted}
                tabIndex={-1} onMouseDown={(event) => event.preventDefault()} onClick={() => selectStock(stock)}>
                <strong>{stock.corp_name}</strong>
                <span>{stock.market && `${stock.market} · `}{stock.stock_code}</span>
              </button>
            </li>
          ))}
        </ul>
      </form>
      {searchError && <p className="error" role="alert">{searchError}</p>}
      {suggestions?.length === 0 && !searchError && <p className="outlook-note" role="status">일치하는 종목이 없습니다.</p>}

      <div className="stock-chips">
        {owned.length > 0 && <div role="group" aria-label="보유 종목">
          <span>보유</span>
          {owned.map((holding) => (
            <button type="button" key={holding.stock_code} aria-pressed={selected.stock_code === holding.stock_code}
              onClick={() => selectStock({ stock_code: holding.stock_code, corp_name: holding.name, market: null })}>
              {holding.name}
            </button>
          ))}
        </div>}
        {watchlist.length > 0 && <div role="group" aria-label="관심 종목">
          <span>관심</span>
          {watchlist.map((stock) => (
            <button type="button" key={stock.stock_code} aria-pressed={selected.stock_code === stock.stock_code}
              onClick={() => selectStock(stock)}>{stock.corp_name}</button>
          ))}
        </div>}
      </div>

      <div className="outlook-heading">
        <div>
          <p className="outlook-kicker">1개월 단기 매매 타점</p>
          <h1 id="price-outlook-title">{band?.name ?? selected.corp_name} <span>({selected.stock_code})</span></h1>
          {band && <p className="outlook-quote">
            <span>분석 시점 <strong>{won.format(band.current_price)}원</strong></span>
            {band.change_pct !== null && <span className="return-badge"
              data-direction={band.change_pct.startsWith("-") ? "loss" : /[1-9]/.test(band.change_pct) ? "gain" : "flat"}>
              전일 대비 {signedPercent(band.change_pct)}
            </span>}
          </p>}
        </div>
        <button type="button" className="outlook-refresh" disabled={running} onClick={() => void analyze(selected, true)}>
          <RefreshCw size={16} aria-hidden="true" /> 다시 분석
        </button>
      </div>
      {running && <div className="outlook-progress" role="status">
        <span className="radar" aria-hidden="true"><span className="animate-ping motion-reduce:animate-none" /></span>
        {band?.name ?? selected.corp_name}의 1개월 단기 저점·고점 타점을 분석하고 있습니다...
      </div>}
      {error && <p className="error" role="alert">{error}</p>}
      {result && <div className="outlook-meta">
        <time dateTime={result.as_of}>{analyzedAt.format(new Date(result.as_of))} 분석 기준</time>
      </div>}
      <TossTenMinuteChart key={selected.stock_code} stockCode={selected.stock_code} band={band}
        pin={pin} pinHeader={pinHeader} onUnauthorized={onUnauthorized} />
      {band && <BandCards band={band} />}
    </section>
  );
}
