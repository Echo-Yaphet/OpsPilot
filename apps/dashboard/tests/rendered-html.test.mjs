import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

async function render() {
  const workerUrl = new URL("../dist/server/index.js", import.meta.url);
  workerUrl.searchParams.set("test", `${process.pid}-${Date.now()}`);
  const { default: worker } = await import(workerUrl.href);
  return worker.fetch(
    new Request("http://localhost/", { headers: { accept: "text/html" } }),
    { ASSETS: { fetch: async () => new Response("Not found", { status: 404 }) } },
    { waitUntil() {}, passThroughOnException() {} },
  );
}

test("server-renders the OpsPilot dashboard shell", async () => {
  const response = await render();
  assert.equal(response.status, 200);
  assert.match(response.headers.get("content-type") ?? "", /^text\/html\b/i);
  const html = await response.text();
  assert.match(html, /<title>OpsPilot · 智能运维控制台<\/title>/i);
  assert.match(html, /运维态势总览/);
  assert.match(html, /启动 Agent 分析/);
  assert.match(html, /故障演练/);
  assert.match(html, /系统正在持续观察/);
});

test("keeps Control API credentials on the Dashboard server", async () => {
  const [page, proxy] = await Promise.all([
    readFile(new URL("../app/page.tsx", import.meta.url), "utf8"),
    readFile(new URL("../app/api/control/[...path]/route.ts", import.meta.url), "utf8"),
  ]);
  assert.match(page, /const API = "\/api\/control"/);
  assert.doesNotMatch(page, /PRIVATE_KEY|Authorization:\s*Bearer|NEXT_PUBLIC_OPSPILOT_API/);
  assert.match(proxy, /OPSPILOT_ACCESS_PRIVATE_KEY_FILE/);
  assert.match(proxy, /roles:\s*\["admin"\]/);
  assert.match(proxy, /jti:\s*randomUUID\(\)/);
  assert.match(proxy, /authorization.*Bearer/si);
  assert.doesNotMatch(proxy, /NEXT_PUBLIC/);
});
