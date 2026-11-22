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
const timestamp = new Intl.DateTimeFormat("ko-KR", { ...seoul, dateStyle: "medium", timeStyle: "short" });
const sessionDate = new Intl.DateTimeFormat("ko-KR", { ...seoul, dateStyle: "medium" });

type Candle = CandlestickData<UTCTimestamp>;
type CandlePayload = { time: string; open: string; high: string; low: string; close: string };

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
  const loadedSession = useRef<string>();
  const [data, setData] = useState<{ daily: boolean; session: string; candles: Candle[] }>();
  const [hoveredTime, setHoveredTime] = useState<number>();
  const [failed, setFailed] = useState(false);
  const latest = data?.candles.at(-1);
  const hovered = data?.candles.find((candle) => candle.time === hoveredTime) ?? latest;
  const buy = band?.levels.buy_zone.price;
  const sell = band?.levels.sell_zone.price;
  const peak = band?.levels.peak_limit.price;

  useEffect(() => {
    if (!container.current) return;
    const instance = createChart(container.current, {
      autoSize: true,
      layout: { background: { type: ColorType.Solid, color: "#FFFFFF" }, textColor: "#8B95A1",
        fontFamily: "Arial, sans-serif", fontSize: 12, attributionLogo: true },
      grid: { horzLines: { color: "#F2F4F6" }, vertLines: { color: "#F2F4F6" } },
      crosshair: { mode: CrosshairMode.Normal, vertLine: { color: "#8B95A1" }, horzLine: { color: "#8B95A1" } },
      rightPriceScale: { borderColor: "#F2F4F6", scaleMargins: { top: 0.13, bottom: 0.13 } },
      timeScale: { borderColor: "#F2F4F6", timeVisible: true, secondsVisible: false, maxBarSpacing: 36,
        tickMarkFormatter: (time: Time, type: TickMarkType) => typeof time === "number"
          ? (type === TickMarkType.Time ? clock : date).format(time * 1000) : null },
      localization: { timeFormatter: (time: UTCTimestamp) => timestamp.format(time * 1000) },
    });
    chart.current = instance;
    series.current = instance.addSeries(CandlestickSeries, {
      upColor: red, downColor: blue, wickUpColor: red, wickDownColor: blue, borderVisible: false,
      lastValueVisible: false, priceLineVisible: false,
      priceFormat: { type: "custom", minMove: 0.01, formatter: (price: number) => `${won.format(price)}원` },
    });
    instance.subscribeCrosshairMove(({ time }) => setHoveredTime(typeof time === "number" ? time : undefined));
    return () => {
      instance.remove();
      chart.current = null;
      series.current = null;
      loadedSession.current = undefined;
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
    if (!data || !chart.current || !series.current) return;
    chart.current.applyOptions({
      localization: { timeFormatter: (time: UTCTimestamp) => (data.daily ? sessionDate : timestamp).format(time * 1000) },
      timeScale: { timeVisible: !data.daily },
    });
    series.current.setData(data.candles);
    const session = data.daily ? "daily" : data.session;
    if (loadedSession.current !== session) {
      chart.current.timeScale().fitContent();
      loadedSession.current = session;
    }
  }, [data]);

  useEffect(() => {
    const controller = new AbortController();
    let pending = false;
    async function refresh() {
      if (pending || controller.signal.aborted) return;
      pending = true;
      try {
        const response = await fetch(`/api/v1/stocks/${encodeURIComponent(stockCode)}/candles?interval=10m`, {
          headers: { [pinHeader]: pin }, signal: controller.signal, cache: "no-store",
        });
        if (controller.signal.aborted) return;
        if (response.status === 401) {
          onUnauthorized("접근이 만료되었습니다. PIN을 다시 입력해 주세요.");
          return;
        }
        if (!response.ok) throw new Error("Candle request failed");
        const rows: CandlePayload[] = await response.json();
        if (!rows.length) throw new Error("No candles");
        const time = rows[rows.length - 1].time;
        const candles = rows.map((row) => ({
          time: (Date.parse(row.time) / 1000) as UTCTimestamp,
          open: Number(row.open), high: Number(row.high), low: Number(row.low), close: Number(row.close),
        }));
        if (controller.signal.aborted) return;
        setData({ daily: time.length === 10, session: time.slice(0, 10), candles });
        setFailed(false);
      } catch {
        if (!controller.signal.aborted) setFailed(true);
      } finally {
        pending = false;
      }
    }
    void refresh();
    const timer = window.setInterval(() => void refresh(), 15_000);
    return () => {
      window.clearInterval(timer);
      controller.abort();
    };
  }, [stockCode, pin, pinHeader, onUnauthorized]);

  return (
    <section className="outlook-chart rounded-2xl bg-white tabular-nums" aria-label="종목 가격 차트">
      <div className="outlook-chart-header">
        <h2>{data?.daily ? "최근 일봉" : "10분봉"}</h2>
        {latest && <p className="outlook-chart-quote">
          {failed || data?.daily ? "최근 종가" : "현재가"} <strong>{won.format(latest.close)}원</strong>
        </p>}
      </div>
      <div className="outlook-chart-details">
        {hovered && <>
          <time dateTime={new Date(hovered.time * 1000).toISOString()}>
            {(data?.daily ? sessionDate : timestamp).format(hovered.time * 1000)}
          </time>
          {([["시가", hovered.open], ["고가", hovered.high], ["저가", hovered.low], ["종가", hovered.close]] as const)
            .map(([label, value]) => <span key={label}>{label} <strong>{won.format(value)}원</strong></span>)}
        </>}
      </div>
      <div className="outlook-chart-plot">
        <div className="outlook-chart-canvas" ref={container} />
        {!latest && <p role="status">{failed ? "가격 차트를 불러올 수 없습니다." : "가격 차트를 불러오는 중입니다."}</p>}
      </div>
      {failed && latest && <p className="outlook-note" role="status">가격 갱신이 지연되고 있습니다.</p>}
    </section>
  );
}
