"use client";

import { useEffect, useRef, useState } from "react";
import {
  CandlestickSeries, ColorType, CrosshairMode, LineStyle, TickMarkType, createChart,
  type AutoscaleInfo, type CandlestickData, type IChartApi, type ISeriesApi, type Time, type UTCTimestamp,
} from "lightweight-charts";
import type { StockPriceBand } from "../lib/chat-stream";

const red = "#F04452";
const blue = "#3182F6";
const won = new Intl.NumberFormat("ko-KR", { maximumFractionDigits: 2 });
const seoul = { timeZone: "Asia/Seoul" };
const clock = new Intl.DateTimeFormat("ko-KR", { ...seoul, hourCycle: "h23", hour: "2-digit", minute: "2-digit" });
const date = new Intl.DateTimeFormat("ko-KR", { ...seoul, month: "numeric", day: "numeric" });
const year = new Intl.DateTimeFormat("ko-KR", { ...seoul, year: "numeric" });
const timestamp = new Intl.DateTimeFormat("ko-KR", { ...seoul, dateStyle: "medium", timeStyle: "short" });
const sessionDate = new Intl.DateTimeFormat("ko-KR", { ...seoul, dateStyle: "medium" });
const intervals = [["10m", "10분봉"], ["1d", "1일봉"], ["1w", "주봉"], ["1y", "1년봉"]] as const;

type Interval = typeof intervals[number][0];
type Candle = CandlestickData<UTCTimestamp>;
type CandlePayload = Pick<Candle, "open" | "high" | "low" | "close"> | Record<"open" | "high" | "low" | "close", string>;

function normalizeCandles(rows: (CandlePayload & { time: number | string })[]) {
  const candles = new Map<number, Candle>();
  for (const row of rows) {
    const time = typeof row.time === "number" ? row.time : Date.parse(row.time) / 1000;
    const [open, high, low, close] = [row.open, row.high, row.low, row.close].map(Number);
    if (Number.isSafeInteger(time) && Math.abs(time) <= 8.64e12 &&
        [open, high, low, close].every((price) => Number.isFinite(price) && price > 0)) {
      candles.set(time, { time: time as UTCTimestamp, open, high, low, close });
    }
  }
  return [...candles.values()].sort((left, right) => left.time - right.time);
}

export default function TossTenMinuteChart({ stockCode, band, pin, pinHeader, onUnauthorized }: {
  stockCode: string;
  band?: StockPriceBand;
  pin: string;
  pinHeader: string;
  onUnauthorized: (message: string) => void;
}) {
  const container = useRef<HTMLDivElement>(null);
  const chart = useRef<IChartApi | null>(null);
  const series = useRef<ISeriesApi<"Candlestick"> | null>(null);
  const [interval, setInterval] = useState<Interval>("10m");
  const [candles, setCandles] = useState<Candle[]>([]);
  const [hoveredTime, setHoveredTime] = useState<number>();
  const [failed, setFailed] = useState(false);
  const [dailyFallback, setDailyFallback] = useState(false);
  const latest = candles.at(-1);
  const hovered = candles.find((candle) => candle.time === hoveredTime) ?? latest;
  const buy = band?.levels.buy_zone.price;
  const sell = band?.levels.sell_zone.price;
  const peak = band?.levels.peak_limit.price;

  useEffect(() => {
    const element = container.current;
    if (!element) return;
    const instance = createChart(element, {
      autoSize: true,
      layout: { background: { type: ColorType.Solid, color: "#FFFFFF" }, textColor: "#8B95A1",
        fontFamily: "Arial, sans-serif", fontSize: 12, attributionLogo: true },
      grid: { horzLines: { color: "#F2F4F6" }, vertLines: { color: "#F2F4F6" } },
      crosshair: { mode: CrosshairMode.Normal, vertLine: { color: "#8B95A1" }, horzLine: { color: "#8B95A1" } },
      rightPriceScale: { borderColor: "#F2F4F6", scaleMargins: { top: 0.13, bottom: 0.13 } },
      timeScale: { borderColor: "#F2F4F6", secondsVisible: false, fixLeftEdge: true, fixRightEdge: true },
    });
    chart.current = instance;
    series.current = instance.addSeries(CandlestickSeries, {
      upColor: red, downColor: blue, wickUpColor: red, wickDownColor: blue, borderVisible: false,
      lastValueVisible: false, priceLineVisible: false,
      priceFormat: { type: "custom", minMove: 0.01, formatter: (price: number) => `${won.format(price)}원` },
    });
    instance.subscribeCrosshairMove(({ time }) => setHoveredTime(typeof time === "number" ? time : undefined));
    const timeScale = instance.timeScale();
    function scrollHistory(event: WheelEvent) {
      if (event.ctrlKey || event.metaKey || !event.deltaY || Math.abs(event.deltaX) >= Math.abs(event.deltaY)) return;
      const range = timeScale.getVisibleLogicalRange();
      const width = timeScale.width();
      if (!range || !width) return;
      const unit = event.deltaMode === WheelEvent.DOM_DELTA_LINE ? 16 : event.deltaMode === WheelEvent.DOM_DELTA_PAGE ? width : 1;
      const position = timeScale.scrollPosition();
      const bars = event.deltaY * unit * (range.to - range.from) / width;
      timeScale.scrollToPosition(position - bars, false);
      event.stopPropagation();
      if (timeScale.scrollPosition() !== position) event.preventDefault();
    }
    element.addEventListener("wheel", scrollHistory, { capture: true, passive: false });
    return () => {
      element.removeEventListener("wheel", scrollHistory, true);
      instance.remove();
      chart.current = null;
      series.current = null;
    };
  }, []);

  useEffect(() => {
    const candleSeries = series.current;
    if (!candleSeries) return;
    const levels = [
      { title: "여기서 사라", price: Number(buy), color: blue },
      { title: "여기서 팔아라", price: Number(sell), color: red },
      { title: "단기 고점", price: Number(peak), color: "#F59E0B" },
    ].filter(({ price }) => Number.isFinite(price) && price > 0);
    candleSeries.applyOptions({
      autoscaleInfoProvider: (base: () => AutoscaleInfo | null) => {
        const info = base();
        if (!info?.priceRange || !levels.length) return info;
        const prices = levels.map(({ price }) => price);
        return { ...info, priceRange: {
          minValue: Math.min(info.priceRange.minValue, ...prices),
          maxValue: Math.max(info.priceRange.maxValue, ...prices),
        } };
      },
    });
    const lines = levels.map((level) => candleSeries.createPriceLine({
      ...level, lineStyle: LineStyle.Dashed, axisLabelVisible: true, axisLabelTextColor: "#FFFFFF",
    }));
    return () => {
      if (series.current === candleSeries) lines.forEach((line) => candleSeries.removePriceLine(line));
    };
  }, [buy, sell, peak]);

  useEffect(() => {
    const instance = chart.current;
    const candleSeries = series.current;
    if (!instance || !candleSeries) return;
    const live = interval === "10m";
    const timeScale = instance.timeScale();
    function formatTimeAxis(showTime: boolean) {
      instance!.applyOptions({
        localization: { timeFormatter: (time: UTCTimestamp) => (showTime ? timestamp : sessionDate).format(time * 1000) },
        timeScale: { timeVisible: showTime, tickMarkFormatter: (time: Time, type: TickMarkType) => {
          if (typeof time !== "number") return null;
          const format = interval === "1y" || type === TickMarkType.Year ? year : type === TickMarkType.Time ? clock : date;
          return format.format(time * 1000);
        } },
      });
    }
    formatTimeAxis(live);
    candleSeries.setData([]);
    setCandles([]);
    setHoveredTime(undefined);
    setFailed(false);
    setDailyFallback(false);

    const controller = new AbortController();
    let timer: number | undefined;
    let previous: Candle[] = [];
    let showingDailyFallback = false;

    function renderCandles(incoming: Candle[]) {
      if (!previous.length) {
        candleSeries!.setData(incoming);
        if (incoming.length > 0) {
          timeScale.setVisibleLogicalRange({ from: Math.max(0, incoming.length - 100) - 0.5, to: incoming.length - 0.5 });
        }
        return incoming;
      }

      // Retain any bars the user could already scroll to when the rolling window moves forward.
      const next = [...previous.filter(({ time }) => time < incoming[0].time), ...incoming];
      if (previous.every((bar, index) => next[index]?.time === bar.time)) {
        next.forEach((bar, index) => {
          const old = previous[index];
          if (!old || bar.open !== old.open || bar.high !== old.high || bar.low !== old.low || bar.close !== old.close) {
            candleSeries!.update(bar, index < previous.length - 1);
          }
        });
      } else {
        candleSeries!.setData(next);
      }
      return next;
    }

    async function refresh() {
      let pollAfter = 15;
      try {
        const response = await fetch(`/api/v1/stocks/${encodeURIComponent(stockCode)}/candles?interval=${interval}`, {
          headers: { [pinHeader]: pin }, signal: controller.signal, cache: "no-store",
        });
        if (controller.signal.aborted) return;
        if (response.status === 401) {
          controller.abort();
          onUnauthorized("접근이 만료되었습니다. PIN을 다시 입력해 주세요.");
          return;
        }
        pollAfter = Math.max(15, Number(response.headers.get("X-Candle-Poll-After")) || 15);
        if (!response.ok) throw new Error("Candle request failed");
        const rows: (CandlePayload & { time: number | string })[] = await response.json();
        const fallback = live && rows.length > 0 && rows.every((row) => typeof row.time === "string");
        const incoming = normalizeCandles(rows);
        if (!incoming.length) throw new Error("No candles");
        if (controller.signal.aborted) return;
        if (fallback !== showingDailyFallback) {
          formatTimeAxis(live && !fallback);
          previous = [];
          setHoveredTime(undefined);
        }
        previous = renderCandles(incoming);
        showingDailyFallback = fallback;
        setDailyFallback(fallback);
        setCandles(previous);
        setFailed(false);
      } catch {
        if (!controller.signal.aborted) setFailed(true);
      } finally {
        if (live && !controller.signal.aborted) timer = window.setTimeout(() => void refresh(), pollAfter * 1000);
      }
    }
    void refresh();
    return () => {
      controller.abort();
      window.clearTimeout(timer);
    };
  }, [stockCode, interval, pin, pinHeader, onUnauthorized]);

  return (
    <section className="outlook-chart rounded-2xl bg-white tabular-nums" aria-label="종목 가격 차트">
      <div className="outlook-chart-header">
        <div className="inline-flex gap-0.5 rounded-xl bg-[#F2F4F6] p-1" role="group" aria-label="봉 간격">
          {intervals.map(([value, label]) => (
            <button key={value} type="button" aria-pressed={value === interval} onClick={() => setInterval(value)}
              className={`whitespace-nowrap rounded-lg px-2.5 py-1.5 font-semibold transition-colors focus-visible:outline-[#3182F6] ${
                value === interval ? "bg-white text-[#191F28] shadow-sm" : "text-[#6B7684] hover:text-[#191F28]"
              }`}>
              {label}
            </button>
          ))}
        </div>
        {latest && <p className="outlook-chart-quote">
          {failed || dailyFallback || interval !== "10m" ? "최근 종가" : "현재가"} <strong>{won.format(latest.close)}원</strong>
        </p>}
      </div>
      <div className="outlook-chart-details">
        {hovered && <>
          <time dateTime={new Date(hovered.time * 1000).toISOString()}>
            {(interval === "10m" && !dailyFallback ? timestamp : sessionDate).format(hovered.time * 1000)}
          </time>
          {([["시가", hovered.open], ["고가", hovered.high], ["저가", hovered.low], ["종가", hovered.close]] as const)
            .map(([label, value]) => <span key={label}>{label} <strong>{won.format(value)}원</strong></span>)}
        </>}
      </div>
      <div className="outlook-chart-plot">
        <div className="outlook-chart-canvas" ref={container} />
        {!latest && <p className="z-10 bg-white/80 text-sm text-[#6B7684]" role="status">
          {failed ? "가격 차트를 불러올 수 없습니다." : "차트 데이터를 불러오는 중입니다..."}
        </p>}
      </div>
      {failed && latest && <p className="outlook-note" role="status">가격 갱신이 지연되고 있습니다.</p>}
      {dailyFallback && <p className="outlook-note" role="status">10분봉을 불러올 수 없어 최근 일봉을 표시합니다.</p>}
    </section>
  );
}
