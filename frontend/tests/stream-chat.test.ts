import assert from "node:assert/strict";
import test from "node:test";
import { SessionExpired, chatRequest, streamChat } from "../lib/chat-stream.ts";

const options = { pin: "test-pin", pinHeader: "X-App-Pin", signal: new AbortController().signal };

async function collect(events: AsyncIterable<unknown>) {
  const items = [];
  for await (const item of events) items.push(item);
  return items;
}

test("streamChat posts JSON with the PIN header and yields the SSE events", async (t) => {
  const upstream = t.mock.method(globalThis, "fetch", async (url: string, init: RequestInit) => {
    assert.equal(url, "/api/chat");
    assert.equal(init.method, "POST");
    assert.equal(init.signal, options.signal);
    assert.deepEqual(init.headers, { "X-App-Pin": "test-pin", "Content-Type": "application/json" });
    assert.equal(init.body, JSON.stringify({ mode: "price_bands", target_stock_code: "005930" }));
    return new Response('event: done\ndata: {"ok":false}\n\n', { headers: { "Content-Type": "text/event-stream" } });
  });
  const events = await collect(streamChat({ mode: "price_bands", target_stock_code: "005930" }, options, "시작 실패"));
  assert.deepEqual(events, [{ event: "done", data: { ok: false } }]);
  assert.equal(upstream.mock.callCount(), 1);
});

test("a chat question carries the stock whose price card is on screen", async (t) => {
  t.mock.method(globalThis, "fetch", async (_url: string, init: RequestInit) => {
    assert.deepEqual(JSON.parse(String(init.body)), { user_query: "추가매수할까? 홀드할까?", selected_stock_code: "005930" });
    return new Response('event: done\ndata: {"ok":false}\n\n', { headers: { "Content-Type": "text/event-stream" } });
  });
  await collect(streamChat(chatRequest("추가매수할까? 홀드할까?", "005930"), options, "시작 실패"));
});

test("a rejected PIN raises SessionExpired and other failures raise the caller's message", async (t) => {
  let status = 401;
  t.mock.method(globalThis, "fetch", async () => new Response("{}", { status }));
  await assert.rejects(collect(streamChat({}, options, "시작 실패")), SessionExpired);
  status = 502;
  await assert.rejects(collect(streamChat({}, options, "시작 실패")),
    (error: Error) => !(error instanceof SessionExpired) && error.message === "시작 실패");
});
