"""Module 5 data collectors. Share the broker's HTTP and Redis clients."""

import asyncio
from collections.abc import Mapping
from datetime import date, datetime, timedelta
from decimal import Decimal, DecimalException, localcontext
from email.utils import parsedate_to_datetime
import html
import json
import logging
import os
import re
from urllib.parse import quote, unquote, urldefrag

import httpx
import psycopg
from psycopg.rows import dict_row
from redis.exceptions import RedisError

from backend import dart_rag
from backend.broker import KST, RETRY_DELAYS, day_string, number, symbol
from backend.finance_calc import CALC_CONTEXT, LockupRelease
from backend.lockup_disclosures import disclosed_releases


FSC_URL = "https://apis.data.go.kr/1160100/GetStocIssuInfoService_V3/getLockUpRetuInfo_V3"
NAVER_URL = "https://openapi.naver.com/v1/search/news.json"
ECOS_URL = "https://ecos.bok.or.kr/api/StatisticSearch"
ECOS_TTL = 86_400
ECOS_SERIES = {
    "usd_krw": ("731Y001", "0000001", "KRW/USD"),
    "base_rate": ("722Y001", "0101000", "%"),
    "treasury_3y": ("817Y002", "010200000", "%"),
    "treasury_10y": ("817Y002", "010210000", "%"),
}
DART_REPORTS = ("증자결정", "증권발행실적보고서")
ZERO = Decimal("0")
_PAYLOAD_ERRORS = (KeyError, TypeError, ValueError, DecimalException)
logger = logging.getLogger(__name__)


class ConnectorError(Exception):
    pass


class ExternalConnectors:
    def __init__(self, http: httpx.AsyncClient, redis, *, fsc_key="", naver_id="",
                 naver_secret="", ecos_key="", clock=None, sleep=asyncio.sleep):
        self.http = http
        self.redis = redis
        self.fsc_key = fsc_key
        self.naver_id = naver_id
        self.naver_secret = naver_secret
        self.ecos_key = ecos_key
        self.clock = clock or (lambda: datetime.now(KST))
        self.sleep = sleep
        self._macro_lock = asyncio.Lock()

    @classmethod
    def from_env(cls, http: httpx.AsyncClient, redis):
        return cls(http, redis, fsc_key=os.getenv("FSC_SERVICE_KEY", "").strip(),
                   naver_id=os.getenv("NAVER_CLIENT_ID", "").strip(),
                   naver_secret=os.getenv("NAVER_CLIENT_SECRET", "").strip(),
                   ecos_key=os.getenv("ECOS_API_KEY", "").strip())

    async def _get_json(self, provider, url, **kwargs):
        for delay in (*RETRY_DELAYS, None):
            try:
                response = await self.http.get(url, timeout=5, **kwargs)
            except httpx.TransportError:
                if delay is None:
                    raise ConnectorError(f"{provider} transport retries exhausted") from None
            else:
                status = response.status_code
                if status not in {408, 429} and status < 500:
                    if not response.is_success:
                        raise ConnectorError(f"{provider} HTTP {status}")
                    try:
                        payload = response.json(parse_float=Decimal)
                    except ValueError:
                        raise ConnectorError(f"{provider} invalid JSON") from None
                    if not isinstance(payload, dict):
                        raise ConnectorError(f"{provider} invalid response object")
                    return payload
                if delay is None:
                    raise ConnectorError(f"{provider} HTTP {status} retries exhausted")
            await self.sleep(delay)

    async def _fsc_releases(self, stock, start, end):
        isin = stock["isin_cd"]
        if not self.fsc_key or not isin:
            return None
        params = {"serviceKey": unquote(self.fsc_key), "resultType": "json",
                  "isinCd": isin, "numOfRows": 1000}
        if stock.get("crno"):
            # The return endpoint also supports issuer queries. Match ISIN locally
            # because a single issuer can have both common and preferred listings.
            params["crno"] = stock["crno"]
        expected = None
        seen = 0
        page = 1
        has_schedule = False
        releases = []
        while True:
            payload = await self._get_json("FSC", FSC_URL, params=params | {"pageNo": page})
            response = payload["response"]
            code = str(response["header"]["resultCode"])
            if code == "03" and page == 1:
                return None
            if code not in {"00", "0"}:
                raise ConnectorError("FSC rejected the ISIN lookup")
            body = response["body"]
            total = int(body["totalCount"])
            items = body.get("items") or {}
            rows = items.get("item", []) if isinstance(items, dict) else items
            if isinstance(rows, dict):
                rows = [rows]
            if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
                raise ConnectorError("FSC invalid release rows")
            if expected is None:
                expected = total
            seen += len(rows)
            if total != expected or seen > total or (not rows and seen < total):
                raise ConnectorError("FSC inconsistent release pagination")
            for row in rows:
                if row.get("isinCd") != isin:
                    continue
                released = date.fromisoformat(str(row["rsrnDt"]))
                shares = number(row["rsrnStckCnt"])
                if shares < ZERO or shares != shares.to_integral_value():
                    raise ConnectorError("FSC invalid release quantity")
                if released >= start:
                    has_schedule = True
                    if released <= end:
                        releases.append(LockupRelease(isin, released, shares))
            if seen == total:
                return releases if has_schedule else None
            page += 1

    async def _dart_releases(self, stock, start, end):
        corp_code = stock.get("corp_code")
        if not corp_code:
            return [], []

        async def search(report):
            try:
                return await dart_rag.search(
                    corp_code, f"{report} {stock['corp_name']} 의무보유 보호예수 해제일 반환 주식수",
                    limit=12, candidates=48,
                )
            except Exception:
                logger.warning("DART lockup retrieval failed for %s", report)
                return []

        pages = await asyncio.gather(*(search(report) for report in DART_REPORTS))
        evidence = {}
        isin = stock["isin_cd"]
        for row in (row for page in pages for row in page):
            report_name = re.sub(r"\s+", "", row["report_nm"])
            if not any(report in report_name for report in DART_REPORTS):
                continue
            if row["rcept_dt"] > start:
                continue
            for released, shares in disclosed_releases(row["chunk_text"], isin, stock["corp_name"]):
                if start <= released <= end:
                    evidence.setdefault((released, shares), set()).add(row["rcept_no"])
        releases = [LockupRelease(isin, released, shares) for released, shares in sorted(evidence)]
        return releases, sorted({receipt for receipts in evidence.values() for receipt in receipts})

    async def get_lockup(self, stock: Mapping, *, as_of: date | None = None) -> dict:
        """Use a Module 1 stock_master row and return Module 3-compatible releases."""
        asset_type = stock["asset_type"]
        result = {"asset_type": asset_type, "applicable": asset_type == "STOCK",
                  "source": None, "status": "unavailable", "release_shares": None,
                  "dilution_pct": None, "releases": [], "dart_receipts": []}
        if not result["applicable"]:
            return result | {"status": "not_applicable", "release_shares": ZERO, "dilution_pct": ZERO}
        start = as_of or self.clock().astimezone(KST).date()
        end = start + timedelta(days=30)
        releases = None
        try:
            releases = await self._fsc_releases(stock, start, end)
        except (ConnectorError, *_PAYLOAD_ERRORS):
            logger.warning("FSC lockup lookup failed")
        source, receipts = "fsc", []
        if releases is None:
            source = "dart_rag"
            try:
                releases, receipts = await self._dart_releases(stock, start, end)
            except _PAYLOAD_ERRORS:
                logger.warning("DART lockup evidence is invalid")
                return result
            if not releases:
                return result
        try:
            with localcontext(CALC_CONTEXT):
                listed = number(stock["listed_shares"])
                if listed <= ZERO:
                    return result
                total = sum((release.shares for release in releases), ZERO)
                dilution = total / listed * Decimal("100")
        except _PAYLOAD_ERRORS:
            logger.warning("Invalid stock_master listed_shares")
            return result
        return result | {"source": source, "status": "ok", "release_shares": total,
                         "dilution_pct": dilution, "releases": releases, "dart_receipts": receipts}

    async def get_news(self, corp_name: str, *, limit: int = 10) -> list[dict]:
        if not self.naver_id or not self.naver_secret or not corp_name.strip():
            return []
        try:
            payload = await self._get_json(
                "Naver", NAVER_URL, params={"query": corp_name, "sort": "date", "display": limit},
                headers={"X-Naver-Client-Id": self.naver_id, "X-Naver-Client-Secret": self.naver_secret},
            )
            items = payload["items"]
            if not isinstance(items, list):
                raise ConnectorError("Naver invalid article list")
        except (ConnectorError, *_PAYLOAD_ERRORS):
            logger.warning("Naver news lookup failed")
            return []

        articles = []
        for row in items:
            try:
                title = _news_text(row["title"])
                summary = _news_text(row["description"])
                original = urldefrag(html.unescape(row.get("originallink") or "").strip()).url
                link = urldefrag(html.unescape(row.get("link") or "").strip()).url
                published = parsedate_to_datetime(row["pubDate"])
                if title and (original or link) and published.utcoffset() is not None:
                    articles.append({"title": title, "description": summary, "link": original or link,
                                     "naver_link": link, "published_at": published.astimezone(KST)})
            except (KeyError, TypeError, ValueError, AttributeError, OverflowError):
                continue
        unique, titles, links = [], set(), set()
        for article in sorted(articles, key=lambda item: item["published_at"], reverse=True):
            title = article["title"].casefold()
            urls = {article["link"], article["naver_link"]} - {""}
            duplicate = title in titles or bool(urls & links)
            titles.add(title)
            links.update(urls)
            if not duplicate:
                unique.append(article)
        return unique[:limit]

    async def _ecos_history(self, table, item, start, end):
        key = quote(self.ecos_key, safe="")
        url = f"{ECOS_URL}/{key}/json/kr/1/100/{table}/D/{day_string(start)}/{day_string(end)}/{item}"
        try:
            payload = await self._get_json("ECOS", url)
            rows = payload["StatisticSearch"]["row"]
            if not isinstance(rows, list):
                raise ConnectorError("ECOS invalid observation list")
        except (ConnectorError, *_PAYLOAD_ERRORS):
            logger.warning("ECOS lookup failed for %s/%s", table, item)
            return {}
        values = {}
        for row in rows:
            try:
                on = date.fromisoformat(str(row["TIME"]))
                if start <= on <= end:
                    values[on.isoformat()] = number(row["DATA_VALUE"])
            except _PAYLOAD_ERRORS:
                continue
        return values

    async def get_macro_context(self, *, as_of: date | None = None) -> dict | None:
        """Latest versus preceding observation and first-to-last change over 30 calendar days."""
        end = as_of or self.clock().astimezone(KST).date()
        start = end - timedelta(days=30)
        key = f"finguard:ecos:macro:v1:{end.isoformat()}"
        async with self._macro_lock:
            try:
                raw = await self.redis.get(key)
                if raw is not None:
                    cached = json.loads(raw)
                    if not isinstance(cached, dict):
                        raise ValueError("invalid ECOS cache")
                    histories = {name: {date.fromisoformat(on).isoformat(): number(value)
                                        for on, value in cached[name].items()} for name in ECOS_SERIES}
                    return _macro_context(end, histories)
            except (RedisError, AttributeError, *_PAYLOAD_ERRORS):
                logger.warning("ECOS Redis cache read failed")
            if not self.ecos_key:
                return None
            series = await asyncio.gather(*(
                self._ecos_history(table, item, start, end) for table, item, _ in ECOS_SERIES.values()
            ))
            histories = dict(zip(ECOS_SERIES, series, strict=True))
            if not any(histories.values()):
                return None
            try:
                await self.redis.set(key, json.dumps(histories, default=str), ex=ECOS_TTL)
            except RedisError:
                logger.warning("ECOS Redis cache write failed")
            return _macro_context(end, histories)

    async def collect(self, stock_code: str) -> dict:
        """Load the canonical stock_master row and collect all available data concurrently."""
        as_of = self.clock().astimezone(KST).date()
        stock = None
        try:
            async with await psycopg.AsyncConnection.connect(row_factory=dict_row) as conn:
                cursor = await conn.execute(
                    "SELECT asset_type, corp_code, isin_cd, corp_name, listed_shares, crno "
                    "FROM stock_master WHERE stock_code = %s", (symbol(stock_code),),
                )
                stock = await cursor.fetchone()
        except psycopg.Error:
            logger.warning("Module 5 stock_master lookup failed")
        if stock is None:
            return {"lockup": None, "news": [], "macro_context": await self.get_macro_context(as_of=as_of)}
        lockup, news, macro = await asyncio.gather(
            self.get_lockup(stock, as_of=as_of), self.get_news(stock["corp_name"]),
            self.get_macro_context(as_of=as_of),
        )
        return {"lockup": lockup, "news": news, "macro_context": macro}


def _news_text(value: str) -> str:
    return " ".join(re.sub(r"</?[a-zA-Z][^<>]*>", "", html.unescape(value)).split())


def _macro_context(as_of, histories):
    context = {"as_of": as_of.isoformat()}
    for name, (_, _, unit) in ECOS_SERIES.items():
        context[name] = None
        history = sorted(histories[name].items())
        if not history:
            continue
        latest_date, latest = history[-1]
        previous_date, previous = history[-2] if len(history) > 1 else (None, None)
        with localcontext(CALC_CONTEXT):
            context[name] = {
                "unit": unit, "change_unit": "percentage_point" if unit == "%" else unit,
                "date": latest_date, "value": latest, "previous_date": previous_date,
                "previous_value": previous, "day_change": latest - previous if previous is not None else None,
                "month_change": latest - history[0][1] if previous is not None else None,
                "history": [{"date": on, "value": value} for on, value in history],
            }
    return context
