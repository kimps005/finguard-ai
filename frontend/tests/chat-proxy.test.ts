import assert from "node:assert/strict";
import test, { type TestContext } from "node:test";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { POST } from "../app/api/chat/route.ts";
import { GET } from "../app/api/account/route.ts";
import { readChatEvents } from "../lib/chat-stream.ts";

const chatBody = JSON.stringify({ user_query: "내 잔고를 알려 줘" });
const encoder = new TextEncoder();

function request(path: string, pin?: string) {
  const headers = new Headers();
  if (pin !== undefined) headers.set("X-App-Pin", pin);
  return new Request(`http://localhost${path}`,
    path === "/api/chat" ? { method: "POST", headers, body: chatBody } : { headers });
}

function configureProxy(t: TestContext) {
  const config = { APP_ACCESS_PIN: "0728", BACKEND_INTERNAL_URL: "http://backend:8000/" };
  const configuredKeys = Object.keys(config);
  const previous = configuredKeys.map((key) => [key, process.env[key]] as const);
  t.after(() => {
    for (const [key, value] of previous) {
      if (value === undefined) delete process.env[key];
      else process.env[key] = value;
    }
  });
  Object.assign(process.env, config);
}

test("both routes refuse missing and incorrect PINs before reaching the backend", async (t) => {
  configureProxy(t);
  const upstream = t.mock.method(globalThis, "fetch", () => assert.fail("unexpected backend request"));
  for (const pin of [undefined, "", "0729", "07280"]) {
    for (const response of [await POST(request("/api/chat", pin)), await GET(request("/api/account", pin))]) {
      assert.equal(response.status, 401);
      assert.deepEqual(await response.json(), { message: "Unauthorized" });
      assert.equal(response.headers.get("Cache-Control"), "no-store");
    }
  }
  delete process.env.APP_ACCESS_PIN;
  assert.equal((await POST(request("/api/chat", "0728"))).status, 401);
  assert.equal((await GET(request("/api/account", "0728"))).status, 401);
  assert.equal(upstream.mock.callCount(), 0);
});

test("a valid PIN forwards intact SSE chunks before upstream closes", { timeout: 2000 }, async (t) => {
  configureProxy(t);

  const first = 'event: status\ndata: {"node":"router_node","phase":"started"}\n\n';
  const second = 'event: token\ndata: {"text":"잔고"}\n\n';
  let stream: ReadableStreamDefaultController<Uint8Array>;
  const body = new ReadableStream<Uint8Array>({
    start(controller) {
      stream = controller;
      stream.enqueue(encoder.encode(first));
    },
  });
  const upstream = t.mock.method(globalThis, "fetch", async (url: URL, init: RequestInit) => {
    assert.equal(url.href, "http://backend:8000/api/v1/chat/stream");
    assert.equal(init.method, "POST");
    assert.equal(init.body, chatBody);
    assert.equal(init.cache, "no-store");
    assert.equal(new Headers(init.headers).get("X-App-Pin"), null);
    return new Response(body, { headers: { "Content-Type": "text/event-stream; charset=utf-8" } });
  });

  const response = await POST(request("/api/chat", "0728"));
  assert.equal(response.status, 200);
  assert.equal(response.headers.get("Content-Type"), "text/event-stream");
  assert.equal(response.headers.get("Cache-Control"), "no-cache, no-transform");
  assert.equal(response.headers.get("X-Accel-Buffering"), "no");
  assert.equal(upstream.mock.callCount(), 1);

  const reader = response.body!.getReader();
  assert.deepEqual(await reader.read(), { done: false, value: encoder.encode(first) });
  stream!.enqueue(encoder.encode(second));
  stream!.close();
  assert.deepEqual(await reader.read(), { done: false, value: encoder.encode(second) });
  assert.equal((await reader.read()).done, true);
});

test("account proxy forwards the lightweight snapshot without caching", async (t) => {
  configureProxy(t);
  const snapshot = { account_summary: { total_value: "1234.56" }, holdings: [] };
  const upstream = t.mock.method(globalThis, "fetch", async (url: URL, init: RequestInit) => {
    assert.equal(url.href, "http://backend:8000/api/v1/account");
    assert.equal(init.cache, "no-store");
    assert.equal(new Headers(init.headers).get("X-App-Pin"), null);
    return Response.json(snapshot);
  });
  const response = await GET(request("/api/account", "0728"));
  assert.equal(response.headers.get("Cache-Control"), "no-store");
  assert.deepEqual(await response.json(), snapshot);
  assert.equal(upstream.mock.callCount(), 1);
});

test("account proxy explains missing settings without exposing upstream details", async (t) => {
  configureProxy(t);
  let problem = { code: "kis_account_not_configured", mode: "live", message: "private-provider-detail" };
  t.mock.method(globalThis, "fetch", async () => Response.json(problem, { status: 503 }));

  for (const [mode, key] of [["live", "KIS_ACCOUNT_NO"], ["virtual", "KIS_VIRTUAL_ACCOUNT_NO"]]) {
    problem = { ...problem, mode };
    const response = await GET(request("/api/account", "0728"));
    assert.equal(response.status, 503);
    assert.equal(response.headers.get("Cache-Control"), "no-store");
    const { message } = await response.json();
    assert.ok(message.includes(key));
    assert.ok(!message.includes(problem.message));
  }

  problem = { ...problem, code: "toss_account_not_configured" };
  const toss = await GET(request("/api/account", "0728"));
  assert.equal(toss.status, 503);
  assert.match((await toss.json()).message, /TOSS_CLIENT_ID.*TOSS_CLIENT_SECRET/);

  problem = { ...problem, code: "unknown_error" };
  const response = await GET(request("/api/account", "0728"));
  assert.equal(response.status, 502);
  assert.ok(!(await response.text()).includes(problem.message));
});

test("SSE reader preserves split UTF-8, line endings, and GFM table tokens", async () => {
  const markdown = "| 종목 | 예상 평단가 |\n| --- | ---: |\n| 종목A | 1,200원 |\n\n- 공시 확인\n";
  const bytes = encoder.encode(`event: token\r\ndata: ${JSON.stringify({ text: markdown })}\r\n\r\n`
    + 'event: done\ndata: {"ok":false}\n\n');
  const body = new ReadableStream<Uint8Array>({
    start(controller) {
      for (const byte of bytes) controller.enqueue(Uint8Array.of(byte));
      controller.close();
    },
  });
  const events = [];
  for await (const event of readChatEvents(body)) events.push(event);
  assert.deepEqual(events, [
    { event: "token", data: { text: markdown } },
    { event: "done", data: { ok: false } },
  ]);
  const token = events[0];
  assert.equal(token.event, "token");
  const html = renderToStaticMarkup(createElement(Markdown, { remarkPlugins: [remarkGfm] }, token.data.text));
  assert.match(html, /<table>/);
  assert.match(html, /<li>공시 확인<\/li>/);
});
