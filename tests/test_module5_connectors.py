import asyncio
from datetime import date, datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock

import httpx
import pytest
from redis.exceptions import ConnectionError

from backend import connectors
from backend.broker import KST, RETRY_DELAYS


AS_OF = date(2026, 11, 21)
STOCK = {"asset_type": "STOCK", "stock_code": "005930", "corp_code": "00126380",
         "isin_cd": "KR7005930003", "corp_name": "삼성전자", "crno": "1301110006246",
         "listed_shares": 3_125_000}


class MemoryRedis:
    def __init__(self):
        self.values = {}
        self.writes = []

    async def get(self, key):
        return self.values.get(key)

    async def set(self, key, value, *, ex):
        self.values[key] = value.encode()
        self.writes.append((key, ex))


def fsc_response(rows, *, total=None):
    return httpx.Response(200, json={"response": {
        "header": {"resultCode": "00"},
        "body": {"totalCount": len(rows) if total is None else total, "items": {"item": rows}},
    }})


@pytest.mark.parametrize("asset_type", ["ETF", "ETN", "REIT"])
def test_non_stock_bypasses_network_and_dart(monkeypatch, asset_type):
    search = AsyncMock()
    monkeypatch.setattr(connectors.dart_rag, "search", search)

    def unexpected(request):
        pytest.fail(f"unexpected HTTP request to {request.url.host}")

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(unexpected)) as http:
            api = connectors.ExternalConnectors(http, MemoryRedis(), fsc_key="test")
            result = await api.get_lockup({"asset_type": asset_type}, as_of=AS_OF)
            assert result["applicable"] is False
            assert result["status"] == "not_applicable"
            assert type(result["dilution_pct"]) is Decimal
            assert result["dilution_pct"] == Decimal("0")
            search.assert_not_awaited()

    asyncio.run(run())


def test_fsc_matches_isin_paginates_and_calculates_decimal_through_day_30(monkeypatch):
    search = AsyncMock()
    monkeypatch.setattr(connectors.dart_rag, "search", search)
    pages = []

    def release(on, shares, isin=STOCK["isin_cd"]):
        return {"isinCd": isin, "rsrnDt": on, "rsrnStckCnt": shares}

    def respond(request):
        assert str(request.url.copy_with(query=None)) == connectors.FSC_URL
        assert request.url.params["isinCd"] == STOCK["isin_cd"]
        assert request.url.params["crno"] == STOCK["crno"]
        assert request.url.params["serviceKey"] == "a+b="
        page = int(request.url.params["pageNo"])
        pages.append(page)
        if page == 1:
            return fsc_response([release("20261121", "25,000"), release("20261120", "900")], total=5)
        return fsc_response([release("20261221", "7"), release("20261222", "800"),
                             release("20261201", "750", "KR7005931001")], total=5)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
            api = connectors.ExternalConnectors(http, MemoryRedis(), fsc_key="a%2Bb%3D")
            result = await api.get_lockup(STOCK, as_of=AS_OF)
            assert pages == [1, 2]
            assert result["source"] == "fsc"
            assert result["release_shares"] == Decimal("25007")
            assert type(result["dilution_pct"]) is Decimal
            assert result["dilution_pct"] == Decimal("0.800224")
            assert [row.release_date for row in result["releases"]] == [AS_OF, date(2026, 12, 21)]
            search.assert_not_awaited()

    asyncio.run(run())


@pytest.mark.parametrize("failure", ["429", "empty"])
def test_failed_isin_lookup_uses_module4_reports(monkeypatch, failure):
    stock = STOCK | {"isin_cd": "KR7005931001", "corp_name": "삼성전자우"}
    report = {"report_nm": "증권발행실적보고서", "rcept_no": "20261106000001", "rcept_dt": date(2026, 11, 6),
              "chunk_text": "| 주식 종류 | 의무보유 해제일 | 의무보유 주식수 |\n"
                            "| --- | --- | --- |\n"
                            "| 우선주 | 2026년 12월 1일 | 12,500 |\n"
                            "| 보통주 | 2026-12-02 | 7,000 |\n"
                            "| 우선주 | 2026-12-31 | 5,000 |"}
    prose = report | {"report_nm": "[기재정정]유상증자 결정", "rcept_no": "20261105000001",
                      "chunk_text": "우선주 12,500주 보호예수 해제 예정일은 2026.12.01이다."}
    future = prose | {"rcept_dt": date(2026, 11, 26), "chunk_text": "우선주 100주 보호예수 해제일 2026.12.07"}
    unrelated = report | {"report_nm": "사업보고서", "chunk_text": "우선주 500주 보호예수 해제일 2026.12.07"}
    search = AsyncMock(side_effect=[[prose, unrelated, future], [report, report]])
    monkeypatch.setattr(connectors.dart_rag, "search", search)
    sleep = AsyncMock()
    attempts = []

    def respond(request):
        attempts.append(request)
        return httpx.Response(429) if failure == "429" else fsc_response([])

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
            api = connectors.ExternalConnectors(http, MemoryRedis(), fsc_key="test", sleep=sleep)
            result = await api.get_lockup(stock, as_of=AS_OF)
            assert result["source"] == "dart_rag"
            assert result["release_shares"] == Decimal("12500")
            assert result["dilution_pct"] == Decimal("0.4")
            assert result["dart_receipts"] == ["20261105000001", "20261106000001"]
            assert len(result["releases"]) == 1
            for call, report_name in zip(search.await_args_list, connectors.DART_REPORTS, strict=True):
                assert call.args[0] == stock["corp_code"]
                assert report_name in call.args[1]
            assert len(attempts) == (4 if failure == "429" else 1)
            assert [call.args[0] for call in sleep.await_args_list] == (
                list(RETRY_DELAYS) if failure == "429" else []
            )

    asyncio.run(run())


@pytest.mark.parametrize(("hub", "url", "id_header", "secret_header"), [
    (False, "https://openapi.naver.com/v1/search/news.json", "X-Naver-Client-Id", "X-Naver-Client-Secret"),
    (True, "https://naverapihub.apigw.ntruss.com/search/v1/news", "X-NCP-APIGW-API-KEY-ID", "X-NCP-APIGW-API-KEY"),
])
def test_naver_news_sanitizes_normalizes_and_deduplicates(monkeypatch, hub, url, id_header, secret_header):
    monkeypatch.setenv("NAVER_CLIENT_ID", "id")
    monkeypatch.setenv("NAVER_CLIENT_SECRET", "secret")
    monkeypatch.setenv("NAVER_API_HUB", str(hub).lower())

    def article(title, link, hour, *, original=""):
        return {"title": title, "link": link, "originallink": original,
                "description": '<b>실적</b> &quot;발표&quot; &amp; 전망',
                "pubDate": f"Fri, 20 Nov 2026 {hour}:00:00 +0000"}

    def respond(request):
        assert str(request.url.copy_with(query=None)) == url
        assert request.url.params["query"] == "삼성전자"
        assert request.url.params["sort"] == "date"
        assert request.headers[id_header] == "id"
        assert request.headers[secret_header] == "secret"
        return httpx.Response(200, json={"items": [
            article("삼성전자 &amp; 반도체", "https://news.test/duplicate-title", "04"),
            article("<b>삼성전자</b> &amp; 반도체", "https://naver.test/1", "06",
                    original="https://news.test/1?a=1&amp;b=2"),
            article("다른 제목", "https://naver.test/1#article", "05"),
            article("&lt;b&gt;삼성전자 업계 동향&lt;/b&gt;", "https://news.test/2", "03"),
            article("종목명 없는 업계 동향", "https://news.test/3", "02"),
            {"title": "잘못된 기사", "pubDate": "invalid"},
        ]})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
            api = connectors.ExternalConnectors.from_env(http, MemoryRedis())
            news = await api.get_news(STOCK["corp_name"])
            # Articles whose title does not name the issuer are dropped as unrelated.
            assert [item["title"] for item in news] == ["삼성전자 & 반도체", "삼성전자 업계 동향"]
            assert news[0] == {"title": "삼성전자 & 반도체", "description": '실적 "발표" & 전망',
                               "link": "https://news.test/1?a=1&b=2", "naver_link": "https://naver.test/1",
                               "published_at": datetime(2026, 11, 20, 15, tzinfo=KST)}
            assert news[0]["published_at"].tzinfo is KST

    asyncio.run(run())


def ecos_response():
    return httpx.Response(200, json={"StatisticSearch": {"row": [
        {"TIME": "20261120", "DATA_VALUE": "3.125"},
        {"TIME": "20261022", "DATA_VALUE": "2.99"},
        {"TIME": "20261119", "DATA_VALUE": "3.100"},
        {"TIME": "20261021", "DATA_VALUE": "2.9"},
    ]}})


def test_ecos_parallel_context_and_24_hour_redis_cache():
    redis = MemoryRedis()
    calls = []

    async def run():
        all_started = asyncio.Event()

        async def respond(request):
            calls.append(request.url.path.split("/")[-1])
            assert "20261022/20261121" in request.url.path
            if len(calls) == 4:
                all_started.set()
            await asyncio.wait_for(all_started.wait(), timeout=2)
            return ecos_response()

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
            api = connectors.ExternalConnectors(
                http, redis, ecos_key="test", clock=lambda: datetime(2026, 11, 20, 15, tzinfo=timezone.utc),
            )
            context, concurrent = await asyncio.gather(api.get_macro_context(), api.get_macro_context())
            second_instance = connectors.ExternalConnectors(http, redis, ecos_key="test")
            assert await second_instance.get_macro_context(as_of=AS_OF) == concurrent == context
            assert sorted(calls) == sorted(item for _, item, _ in connectors.ECOS_SERIES.values())
            assert redis.writes == [("finguard:ecos:macro:v1:2026-11-21", 86_400)]
            assert context["as_of"] == "2026-11-21"
            assert set(context) == {"as_of", "usd_krw", "base_rate", "treasury_3y", "treasury_10y"}
            assert context["usd_krw"]["change_unit"] == "KRW/USD"
            assert context["treasury_10y"]["change_unit"] == "percentage_point"
            rate = context["base_rate"]
            assert rate["date"] == "2026-11-20"
            assert rate["previous_date"] == "2026-11-19"
            assert rate["day_change"] == Decimal("0.025")
            assert rate["month_change"] == Decimal("0.135")
            assert [item["value"] for item in rate["history"]] == [
                Decimal("2.99"), Decimal("3.100"), Decimal("3.125"),
            ]
            assert type(concurrent["base_rate"]["value"]) is Decimal

    asyncio.run(run())


def test_timeout_and_http_5xx_retries_with_ecos_and_redis_partial_failure(monkeypatch):
    monkeypatch.setattr(connectors.dart_rag, "search", AsyncMock(side_effect=RuntimeError))
    redis = MemoryRedis()
    redis.get = AsyncMock(side_effect=ConnectionError)
    redis.set = AsyncMock(side_effect=ConnectionError)
    sleep = AsyncMock()
    attempts = {}

    def respond(request):
        endpoint = request.url.path.rsplit("/", 1)[-1]
        attempts[endpoint] = attempts.get(endpoint, 0) + 1
        if endpoint == "010210000":
            raise httpx.ReadTimeout("test", request=request)
        if endpoint in {"news.json", "getLockUpRetuInfo_V3"}:
            return httpx.Response(503)
        return ecos_response()

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
            api = connectors.ExternalConnectors(http, redis, fsc_key="test", ecos_key="test",
                                                naver_id="test", naver_secret="test", sleep=sleep,
                                                clock=lambda: datetime(2026, 11, 21, tzinfo=KST))
            database = AsyncMock()
            database.__aenter__.return_value = database
            database.execute.return_value.fetchone.return_value = STOCK
            connect = AsyncMock(return_value=database)
            monkeypatch.setattr(connectors.psycopg.AsyncConnection, "connect", connect)
            result = await api.collect("A005930")
            assert database.execute.await_args.args[1] == ("005930",)
            assert result["lockup"]["status"] == "unavailable"
            assert result["lockup"]["dilution_pct"] is None
            assert result["news"] == []
            assert result["macro_context"]["treasury_10y"] is None
            assert result["macro_context"]["usd_krw"]["value"] == Decimal("3.125")
            assert [attempts[key] for key in ("news.json", "getLockUpRetuInfo_V3", "010210000")] == [4] * 3
            assert sorted(call.args[0] for call in sleep.await_args_list) == sorted(RETRY_DELAYS * 3)
            assert redis.set.await_args.kwargs["ex"] == 86_400

    asyncio.run(run())
