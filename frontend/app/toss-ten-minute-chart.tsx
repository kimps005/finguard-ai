"use client";

import { useEffect, useRef, useState } from "react";
import {
  CandlestickSeries, ColorType, CrosshairMode, LineStyle, TickMarkType, createChart,
  type AutoscaleInfo, type CandlestickData, type IChartApi, type ISeriesApi, type Time, type UTCTimestamp,
} from "lightweight-charts";
import type { StockPriceBand } from "../lib/chat-stream";

// Mirrors the --up / --down / --muted tokens in globals.css (Korean convention: red rises, blue falls).
const red = "#D92D3A";
const blue = "#2F64D8";
const neutral = "#7D8592";
const grid = "#EEF0F3";
const axis = "#8A919C";
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
      layout: { background: { type: ColorType.Solid, color: "#FFFFFF" }, textColor: axis,
        fontFamily: getComputedStyle(element).fontFamily, fontSize: 11, attributionLogo: true },
      grid: { horzLines: { color: grid }, vertLines: { visible: false } },
      crosshair: { mode: CrosshairMode.Normal,
        vertLine: { color: axis, labelBackgroundColor: "#1B2330" }, horzLine: { color: axis, labelBackgroundColor: "#1B2330" } },
      rightPriceScale: { borderVisible: false, scaleMargins: { top: 0.12, bottom: 0.12 } },
      timeScale: { borderColor: grid, secondsVisible: false,
        barSpacing: 10, minBarSpacing: 7, maxBarSpacing: 13, rightOffset: 4 },
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
      { title: "매수", price: Number(buy), color: blue },
      { title: "매도", price: Number(sell), color: red },
      { title: "상단", price: Number(peak), color: neutral },
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
    instance.applyOptions({
      localization: { timeFormatter: (time: UTCTimestamp) => (live ? timestamp : sessionDate).format(time * 1000) },
      timeScale: { timeVisible: live, tickMarkFormatter: (time: Time, type: TickMarkType) => {
        if (typeof time !== "number") return null;
        const format = interval === "1y" || type === TickMarkType.Year ? year : type === TickMarkType.Time ? clock : date;
        return format.format(time * 1000);
      } },
    });
    candleSeries.setData([]);
    setCandles([]);
    setHoveredTime(undefined);
    setFailed(false);

    const controller = new AbortController();
    let timer: number | undefined;
    let previous: Candle[] = [];

    function renderCandles(incoming: Candle[]) {
      if (!previous.length) {
        candleSeries!.setData(incoming);
        if (incoming.length >= 80) {
          timeScale.setVisibleLogicalRange({ from: Math.max(0, incoming.length - 80), to: incoming.length + 3 });
        } else {
          timeScale.resetTimeScale();
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
        const incoming = normalizeCandles(live ? rows.filter(({ time }) => typeof time === "number") : rows);
        if (!incoming.length) throw new Error("No candles");
        if (controller.signal.aborted) return;
        previous = renderCandles(incoming);
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

  const live = interval === "10m" && !failed;
  return (
    <section className="chart" aria-label="가격 차트">
      <div className="chart-head">
        <div className="segmented" role="group" aria-label="봉 간격">
          {intervals.map(([value, label]) => (
            <button key={value} type="button" aria-pressed={value === interval} onClick={() => setInterval(value)}>
              {label}
            </button>
          ))}
        </div>
        {latest && <p className="chart-quote">
          <span className="chart-state" data-live={live}>{live ? "실시간" : "최근 종가"}</span>
          <strong>{won.format(latest.close)}<small>원</small></strong>
        </p>}
      </div>
      <dl className="chart-ohlc" aria-live="off">
        {hovered && <>
          <div className="chart-ohlc-time"><dt className="sr-only">시각</dt><dd>
            <time dateTime={new Date(hovered.time * 1000).toISOString()}>
              {(interval === "10m" ? timestamp : sessionDate).format(hovered.time * 1000)}
            </time>
          </dd></div>
          {([["시가", hovered.open], ["고가", hovered.high], ["저가", hovered.low], ["종가", hovered.close]] as const)
            .map(([label, value]) => <div key={label}><dt>{label}</dt><dd>{won.format(value)}</dd></div>)}
        </>}
      </dl>
      <div className="chart-plot">
        <div className="chart-canvas" ref={container} />
        {!latest && <p className="chart-overlay" role="status">
          {failed ? interval === "10m" ? "가격 차트를 불러올 수 없습니다. 잠시 후 자동으로 다시 시도합니다." : "가격 차트를 불러올 수 없습니다. 다른 봉 간격을 선택해 보세요." : "차트를 불러오는 중입니다."}
        </p>}
      </div>
      {failed && latest && <p className="inline-note" role="status">가격 갱신이 지연되고 있습니다. 마지막으로 받은 가격을 표시합니다.</p>}
    </section>
  );
}
