/**
 * Смоук-тест Node SDK против живого API.
 *
 *   export NEXUSAPI_API_KEY=...
 *   (cd node && npm ci && npm run build)
 *   node smoke/node_smoke.mjs            # бесплатные проверки
 *   node smoke/node_smoke.mjs --paid     # + платные, около 1 ₽
 *
 * Берёт SDK из ./node/dist (текущий код репозитория). Ключ не печатает.
 * Код выхода 0 — все проверки прошли.
 */
import { randomUUID } from "node:crypto";
import { NexusClient, NotFoundError, VERSION, webhookSecretFromApiKey } from "../node/dist/index.js";

const CHAT_MODEL = "gpt-6-luna"; // самая дешёвая текстовая модель
const IMAGE_MODEL = "nano-banana"; // самая дешёвая картинка (0,8 ₽)
const paid = process.argv.includes("--paid");
const apiKey = process.env.NEXUSAPI_API_KEY;
if (!apiKey) {
  console.log("set NEXUSAPI_API_KEY");
  process.exit(2);
}

const results = [];
async function check(name, fn) {
  const started = Date.now();
  try {
    const detail = await fn();
    console.log(`✓ ${name} (${((Date.now() - started) / 1000).toFixed(1)}s): ${detail}`);
    results.push(true);
  } catch (e) {
    const rid = e?.requestId ? ` (requestId=${e.requestId})` : "";
    console.log(`✗ ${name}: ${e?.name ?? "Error"}: ${e?.message ?? e}${rid}`);
    results.push(false);
  }
}

async function expectNotFound(fn) {
  try {
    await fn();
  } catch (e) {
    if (!(e instanceof NotFoundError)) throw e;
    if (!e.requestId) throw new Error("404 without X-Request-Id");
    return `404, requestId=${e.requestId}`;
  }
  throw new Error("expected NotFoundError");
}

console.log(`nexusapi-sdk ${VERSION} (node) → ${process.env.NEXUSAPI_BASE_URL || "https://nexusapi.dev"}`);
const client = new NexusClient();

await check("public catalog", async () => `${(await client.models.list()).length} models`);
await check("auth + /v1/models", async () => `${(await client.v1.models.list()).length} models for this key`);
await check("webhook secret", async () => {
  if ((await client.webhooks.secret()) !== (await webhookSecretFromApiKey(apiKey))) {
    throw new Error("server secret differs from SDK derivation");
  }
  return "matches SDK derivation";
});
await check("unknown task → 404", () => expectNotFound(() => client.tasks.get(`smoke-${randomUUID()}`)));
await check("unknown chat model → 404", () =>
  expectNotFound(() => client.chat.completions.create({ model: "no-such-model", messages: [] })),
);

if (paid) {
  const messages = [{ role: "user", content: "Reply with exactly one word: pong" }];
  await check(`chat ${CHAT_MODEL}`, async () => {
    const reply = await client.chat.completions.create({ model: CHAT_MODEL, messages, max_tokens: 200 });
    const text = reply.choices[0]?.message.content ?? "";
    if (!text.trim()) throw new Error(`empty content: ${JSON.stringify(reply)}`);
    return `${JSON.stringify(text.trim().slice(0, 40))}, usage=${JSON.stringify(reply.usage)}`;
  });
  await check(`chat stream ${CHAT_MODEL}`, async () => {
    const stream = await client.chat.completions.create({ model: CHAT_MODEL, messages, max_tokens: 200, stream: true });
    const final = await stream.finalCompletion();
    const text = final.choices[0]?.message.content ?? "";
    if (!text.trim()) throw new Error(`empty stream: ${JSON.stringify(final)}`);
    return `${JSON.stringify(text.trim().slice(0, 40))}, finish=${final.choices[0]?.finish_reason}`;
  });
  await check(`image ${IMAGE_MODEL} + idempotent replay`, async () => {
    const idempotencyKey = `smoke-${randomUUID()}`;
    const params = { model: IMAGE_MODEL, prompt: "A small red cube", wait: false, idempotencyKey };
    const first = await client.images.generate(params);
    const second = await client.images.generate(params);
    if (first.id !== second.id) throw new Error(`same key gave two tasks: ${first.id} vs ${second.id}`);
    await first.wait({ timeoutMs: 300_000, pollIntervalMs: 2_000 });
    if (!first.imageUrl) throw new Error(`no image url: ${JSON.stringify(first.result)}`);
    return `one task ${first.id} for two requests, ${first.imageUrl.slice(0, 60)}…`;
  });
}

const failed = results.filter((r) => !r).length;
console.log(`\n${results.length - failed}/${results.length} passed`);
process.exit(failed ? 1 : 0);
