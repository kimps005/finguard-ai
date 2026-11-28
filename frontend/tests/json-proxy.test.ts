import assert from "node:assert/strict";
import test, { type TestContext } from "node:test";
import { GET as searchStocks } from "../app/api/stocks/search/route.ts";
import { GET as candles } from "../app/api/v1/stocks/[stock_code]/candles/route.ts";

function configureProxy(t: TestContext) {
  const previous = { pin: process.env.APP_ACCESS_PIN, url: process.env.BACKEND_INTERNAL_URL };
  t.after(() => {
    for (const [key, value] of [["APP_ACCESS_PIN", previous.pin], ["BACKEND_INTERNAL_URL", previous.url]]) {
      if (value === undefined) delete process.env[key as string];
      else process.env[key as string] = value;
    }
  });
  process.env.APP_ACCESS_PIN = "test-pin";
  process.env.BACKEND_INTERNAL_URL = "http://backend:8000/";
}

function get(path: string, pin: string | undefined = "test-pin") {
  return new Request(`http://localhost${path}`, { headers: pin === undefined ? {} : { "X-App-Pin": pin } });
}

const candleRoute = (path: string, code = "005930", pin?: string) =>
  candles(get(path, pin), { params: { stock_code: code } });

test("search and candle routes refuse bad PINs and invalid input before the backend", async (t) => {
  configureProxy(t);
  const upstream = t.mock.method(globalThis, "fetch", () => assert.fail("unexpected backend request"));
  for (const response of [await searchStocks(get("/api/stocks/search?q=삼성", "test-pix")),
                          await candleRoute("/api/v1/stocks/005930/candles", "005930", "")]) {
    assert.equal(response.status, 401);
    assert.equal(response.headers.get("Cache-Control"), "no-store");
  }
  for (const response of [await searchStocks(get("/api/stocks/search?q=%20")),
                          await searchStocks(get(`/api/stocks/search?q=${"가".repeat(101)}`)),
                          await candleRoute("/api/v1/stocks/00593/candles", "00593"),
                          await candleRoute("/api/v1/stocks/005930/candles?interval=5m")]) {
    assert.equal(response.status, 422);
  }
  assert.equal(upstream.mock.callCount(), 0);
});

test("search forwards the trimmed query and the JSON result without caching", async (t) => {
  configureProxy(t);
  const rows = [{ stock_code: "005930", corp_name: "삼성전자", market: "KOSPI" }];
  t.mock.method(globalThis, "fetch", async (url: URL, init: RequestInit) => {
    assert.equal(url.href, "http://backend:8000/api/v1/stocks/search?q=%EC%82%BC%EC%84%B1");
    assert.equal(init.cache, "no-store");
    assert.equal(new Headers(init.headers).get("X-App-Pin"), null);
    return Response.json(rows);
  });
  const response = await searchStocks(get("/api/stocks/search?q=%20삼성%20"));
  assert.equal(response.status, 200);
  assert.equal(response.headers.get("Cache-Control"), "no-store");
  assert.equal(response.headers.get("Content-Type"), "application/json");
  assert.deepEqual(await response.json(), rows);
});

test("candles keep the poll hint on success and on upstream failure", async (t) => {
  configureProxy(t);
  let status = 200;
  t.mock.method(globalThis, "fetch", async (url: URL) => {
    assert.equal(url.href, "http://backend:8000/api/v1/stocks/005930/candles?interval=10m");
    const headers = { "X-Candle-Poll-After": "15" };
    return status === 200 ? Response.json([{ time: 1 }], { headers })
      : Response.json({ detail: "private-provider-detail" }, { status, headers });
  });
  const ok = await candleRoute("/api/v1/stocks/005930/candles");
  assert.equal(ok.status, 200);
  assert.equal(ok.headers.get("X-Candle-Poll-After"), "15");
  assert.deepEqual(await ok.json(), [{ time: 1 }]);

  status = 502;
  const failed = await candleRoute("/api/v1/stocks/005930/candles");
  assert.equal(failed.status, 502);
  assert.equal(failed.headers.get("X-Candle-Poll-After"), "15");
  assert.deepEqual(await failed.json(), { message: "가격 차트를 불러올 수 없습니다." });
});

test("unreachable or failing backends map to fixed Korean messages", async (t) => {
  configureProxy(t);
  let fail = true;
  t.mock.method(globalThis, "fetch", async () => {
    if (fail) throw new TypeError("fetch failed: connect ECONNREFUSED backend:8000");
    return new Response("<html>private stack trace</html>", { status: 500 });
  });
  const offline = [await searchStocks(get("/api/stocks/search?q=삼성")), await candleRoute("/api/v1/stocks/005930/candles?interval=1d")];
  assert.deepEqual(await Promise.all(offline.map((response) => response.json())), [
    { message: "종목 검색 서버에 연결할 수 없습니다." }, { message: "가격 차트 서버에 연결할 수 없습니다." }]);

  fail = false;
  const failed = await searchStocks(get("/api/stocks/search?q=삼성"));
  assert.equal(failed.status, 502);
  assert.deepEqual(await failed.json(), { message: "종목 검색을 완료할 수 없습니다." });
});
