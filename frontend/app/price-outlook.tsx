"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { RefreshCw } from "lucide-react";
import { readChatEvents, type DecimalString, type PriceBand, type PriceBands } from "../lib/chat-stream";

export const PRICE_OUTLOOK_KEY = "finguard-price-outlook-v1";
const won = new Intl.NumberFormat("ko-KR", { maximumFractionDigits: 2 });
const analyzedAt = new Intl.DateTimeFormat("ko-KR", {
  dateStyle: "medium", timeStyle: "short", timeZone: "Asia/Seoul",
});

function signedPercent(value: DecimalString | null) {
  if (value === null) return "자료 없음";
  const nonzero = /[1-9]/.test(value);
  return nonzero ? `${value.startsWith("-") ? "" : "+"}${won.format(value)}%` : "0%";
}

function readCache(): PriceBands | "started" | null {
  try {
    const cached = JSON.parse(sessionStorage.getItem(PRICE_OUTLOOK_KEY) ?? "null");
    if (cached === "started") return cached;
    return cached && typeof cached.as_of === "string" && Number.isFinite(Date.parse(cached.as_of))
      && Array.isArray(cached.bands) ? cached : null;
  } catch {
    return null;
  }
}

function BandCards({ band }: { band: PriceBand }) {
  if (!band.levels) return <p className="mt-4 text-sm">현재가를 확인할 수 없어 이 종목의 가격 밴드를 표시할 수 없습니다.</p>;
  const { buy, sell, ceiling } = band.levels;
  const cards = [
    { key: "buy", label: "단기 예상 저점", action: "여기서 사라", level: buy,
      metrics: [`현재가 대비 ${signedPercent(buy.current_pct)}`] },
    { key: "sell", label: "단기 예상 고점", action: "여기서 팔아라", level: sell,
      metrics: [`현재가 대비 ${signedPercent(sell.current_pct)}`, `평단가 대비 ${signedPercent(sell.average_pct)}`] },
    { key: "ceiling", label: "1개월 상단 한계", action: "단기적으로 여기가 고점이다", level: ceiling, metrics: [] },
  ];
  return (
    <>
      {band.indicative && <p className="outlook-note">최근 변동폭 자료가 부족해 현재가 기준 참고 가격을 표시합니다.</p>}
      <div className="outlook-cards">
        {cards.map(({ key, label, action, level, metrics }) => (
          <article key={key} className="outlook-card rounded-2xl bg-white tabular-nums" data-level={key}>
            <p className="outlook-card-label">{label}</p>
            <h3>{action}</h3>
            <p className="outlook-price">{won.format(level.price)}<span>원</span></p>
            {metrics.length > 0 && <div className="outlook-returns">{metrics.map((metric) => <span key={metric}>{metric}</span>)}</div>}
            <p className="outlook-reason">{level.reason}</p>
          </article>
        ))}
      </div>
      <div className="outlook-gauge rounded-2xl bg-white" role="img"
        aria-label={`저점 ${won.format(buy.price)}원, 분석 시점 현재가 ${won.format(band.current_price)}원, 고점 ${won.format(sell.price)}원. 밴드 위치 ${band.position_pct}%`}>
        <p>분석 시점 현재가 <strong>{won.format(band.current_price)}원</strong></p>
        <div className="outlook-track" aria-hidden="true">
          <span className="outlook-marker" style={{ left: `${band.position_pct}%` }} />
        </div>
        <div className="outlook-endpoints" aria-hidden="true">
          <span>여기서 사라 <strong>{won.format(buy.price)}원</strong></span>
          <span>여기서 팔아라 <strong>{won.format(sell.price)}원</strong></span>
        </div>
      </div>
      {(band.ma20 !== null || band.recent_range || band.flow || band.disclosures.length > 0) && (
        <details className="outlook-evidence rounded-2xl bg-white">
          <summary>핵심 근거</summary>
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
        </details>
      )}
    </>
  );
}

export default function PriceOutlook({ pin, pinHeader, manualRunning, onBusyChange, onUnauthorized }: {
  pin: string;
  pinHeader: string;
  manualRunning: boolean;
  onBusyChange: (busy: boolean) => void;
  onUnauthorized: (message: string) => void;
}) {
  const [result, setResult] = useState<PriceBands | null>(null);
  const [selected, setSelected] = useState("");
  const [running, setRunning] = useState(true);
  const [error, setError] = useState("");
  const request = useRef<AbortController | null>(null);

  const show = useCallback(({ as_of, bands }: PriceBands) => {
    const snapshot = { as_of, bands };
    setResult(snapshot);
    try {
      sessionStorage.setItem(PRICE_OUTLOOK_KEY, JSON.stringify(snapshot));
    } catch {
      // The visible result is still usable when browser storage is unavailable.
    }
  }, []);

  const analyze = useCallback(async () => {
    if (request.current) return;
    const controller = new AbortController();
    request.current = controller;
    setRunning(true);
    onBusyChange(true);
    setError("");
    try {
      try {
        if (!readCache()) sessionStorage.setItem(PRICE_OUTLOOK_KEY, JSON.stringify("started"));
      } catch {
        // Continue the analysis if browser storage is unavailable.
      }
      const response = await fetch("/api/chat", {
        method: "POST",
        headers: { [pinHeader]: pin, "Content-Type": "application/json" },
        body: JSON.stringify({ mode: "price_bands" }),
        signal: controller.signal,
      });
      if (response.status === 401) {
        onUnauthorized("접근이 만료되었습니다. PIN을 다시 입력해 주세요.");
        return;
      }
      if (!response.ok || !response.body) throw new Error("타점 분석을 시작할 수 없습니다. 다시 분석해 주세요.");
      for await (const item of readChatEvents(response.body)) {
        if (controller.signal.aborted) return;
        if (item.event === "price_bands") show(item.data);
        if (item.event === "error") throw new Error(item.data.message);
        if (item.event === "done") {
          if (!item.data.ok || !("mode" in item.data)) throw new Error("타점 분석을 완료할 수 없습니다.");
          show(item.data);
          return;
        }
      }
      throw new Error("타점 분석이 끝나기 전에 연결이 끊겼습니다.");
    } catch (problem) {
      if (!controller.signal.aborted) setError(problem instanceof Error ? problem.message : "타점 분석을 완료할 수 없습니다.");
    } finally {
      if (request.current === controller) {
        request.current = null;
        setRunning(false);
        onBusyChange(false);
      }
    }
  }, [onBusyChange, onUnauthorized, pin, pinHeader, show]);

  useEffect(() => {
    const cached = readCache();
    let launch: number | undefined;
    if (cached) {
      if (cached === "started") setError("완료된 타점 분석이 없습니다. 다시 분석을 눌러 주세요.");
      else setResult(cached);
      setRunning(false);
    } else {
      onBusyChange(true);
      launch = window.setTimeout(() => void analyze(), 0);
    }
    return () => {
      window.clearTimeout(launch);
      request.current?.abort();
      request.current = null;
    };
  }, [analyze, onBusyChange]);

  const band = result?.bands.find(({ stock_code }) => stock_code === selected) ?? result?.bands[0];
  return (
    <section className="price-outlook" aria-labelledby="price-outlook-title">
      <div className="outlook-heading">
        <h2 id="price-outlook-title">1개월 단기 매매 타점</h2>
        <button type="button" className="outlook-refresh" disabled={running || manualRunning} onClick={() => void analyze()}>
          <RefreshCw size={16} aria-hidden="true" /> 다시 분석
        </button>
      </div>
      {running && (
        <div className="outlook-progress" role="status">
          <span className="radar" aria-hidden="true"><span className="animate-ping motion-reduce:animate-none" /></span>
          보유 종목의 1개월 단기 저점·고점 타점을 계산하고 있습니다...
        </div>
      )}
      {error && <p className="error" role="alert">{error}</p>}
      {result && <div className="outlook-meta">
        {band && (result.bands.length > 1 ? (
          <select aria-label="분석 보유 종목" value={band.stock_code} onChange={(event) => setSelected(event.target.value)}>
            {result.bands.map(({ stock_code, name }) => <option key={stock_code} value={stock_code}>{name} ({stock_code})</option>)}
          </select>
        ) : <strong>{band.name} <span className="ticker">{band.stock_code}</span></strong>)}
        <time dateTime={result.as_of}>{analyzedAt.format(new Date(result.as_of))} 기준</time>
      </div>}
      {band && <BandCards band={band} />}
      {result?.bands.length === 0 && <p className="outlook-note">분석할 보유 종목이 없습니다.</p>}
    </section>
  );
}
