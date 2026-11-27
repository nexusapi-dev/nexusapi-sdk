/**
 * 0.4.0: конфигурация (env, withOptions, defaultHeaders, опции вызова),
 * иерархия ошибок и requestId, подпись вебхуков.
 */
import { describe, it, beforeEach, afterEach } from "node:test";
import assert from "node:assert/strict";
import { createHmac } from "node:crypto";
import {
  APIStatusError,
  APITimeoutError,
  AuthenticationError,
  InternalServerError,
  NetworkError,
  NexusClient,
  NexusError,
  ValidationError,
  WebhookVerificationError,
  unwrapWebhook,
  verifyWebhookSignature,
  webhookSecretFromApiKey,
} from "../dist/index.js";

function mockFetch(responder) {
  const calls = [];
  const fetchMock = async (url, init) => {
    const call = { url: String(url), method: init?.method ?? "GET", headers: init?.headers ?? {}, signal: init?.signal };
    calls.push(call);
    return responder(call, calls.length);
  };
  return { fetchMock, calls };
}

const json = (status, body, headers) =>
  new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json", ...(headers ?? {}) } });

const client = (fetchMock, extra = {}) =>
  new NexusClient({ apiKey: "k-123", baseUrl: "https://api.test", maxRetries: 0, fetch: fetchMock, ...extra });

const TASK = { task_id: "t", status: "pending" };

// ============================================================
// Конфигурация
// ============================================================

describe("configuration", () => {
  const saved = {};
  beforeEach(() => {
    for (const k of ["NEXUSAPI_API_KEY", "NEXUSAPI_BASE_URL"]) {
      saved[k] = process.env[k];
      delete process.env[k];
    }
  });
  afterEach(() => {
    for (const [k, v] of Object.entries(saved)) {
      if (v === undefined) delete process.env[k];
      else process.env[k] = v;
    }
  });

  it("reads apiKey and baseUrl from the environment", async () => {
    process.env.NEXUSAPI_API_KEY = "env-key-1";
    process.env.NEXUSAPI_BASE_URL = "https://env.test/";
    const { fetchMock, calls } = mockFetch(() => json(200, TASK));
    await new NexusClient({ fetch: fetchMock }).tasks.get("t");
    assert.equal(calls[0].url, "https://env.test/tasks/t");
    assert.equal(calls[0].headers.Authorization, "Bearer env-key-1");
  });

  it("missing key mentions the env variable", () => {
    assert.throws(() => new NexusClient(), (e) => e instanceof AuthenticationError && /NEXUSAPI_API_KEY/.test(e.message));
  });

  it("defaultHeaders and withOptions share fetch, merge headers, override retries", async () => {
    const { fetchMock, calls } = mockFetch((_, n) => (n === 1 ? json(503, { detail: "busy" }) : json(200, TASK)));
    const base = client(fetchMock, { maxRetries: 2, defaultHeaders: { "X-Team": "media" } });
    const quick = base.withOptions({ maxRetries: 0, defaultHeaders: { "X-Trace": "1" } });
    await assert.rejects(quick.tasks.get("t"), InternalServerError); // без повтора
    assert.equal(calls.length, 1);
    assert.equal(calls[0].headers["X-Team"], "media");
    assert.equal(calls[0].headers["X-Trace"], "1");
    assert.equal((await base.tasks.get("t")).id, "t");
  });

  it("per-call options: headers, maxRetries, timeoutMs, signal", { timeout: 5_000 }, async () => {
    const { fetchMock, calls } = mockFetch(() => json(503, { detail: "busy" }, { "retry-after": "0" }));
    const c = client(fetchMock, { maxRetries: 2 });
    await assert.rejects(c.tasks.get("t", { maxRetries: 0, headers: { "X-Trace": "abc" } }), InternalServerError);
    assert.equal(calls.length, 1);
    assert.equal(calls[0].headers["X-Trace"], "abc");

    const hanging = (url, init) =>
      new Promise((_, reject) => init.signal.addEventListener("abort", () => reject(init.signal.reason ?? new Error("aborted"))));
    await assert.rejects(
      client(hanging).v1.models.list({ timeoutMs: 50 }),
      (e) => e instanceof APITimeoutError && e instanceof NetworkError,
    );
    const ac = new AbortController();
    setTimeout(() => ac.abort(), 20);
    await assert.rejects(client(hanging).videos.create({ model: "m", prompt: "p" }, { signal: ac.signal }), (e) => e.name === "AbortError");
  });
});

// ============================================================
// Ошибки
// ============================================================

describe("error hierarchy", () => {
  for (const [status, Cls] of [
    [400, ValidationError],
    [401, AuthenticationError],
    [424, APIStatusError],
    [500, InternalServerError],
    [502, InternalServerError],
  ]) {
    it(`HTTP ${status} → ${Cls.name} with requestId/code/type/param`, async () => {
      const { fetchMock } = mockFetch(() =>
        json(
          status,
          { error: { message: "boom", type: "invalid_request_error", code: "bad_size", param: "size" } },
          { "x-request-id": "req_42" },
        ),
      );
      await assert.rejects(client(fetchMock).tasks.get("t"), (e) => {
        assert.ok(e instanceof Cls && e instanceof APIStatusError && e instanceof NexusError);
        assert.equal(e.constructor, Cls);
        assert.deepEqual([e.requestId, e.code, e.type, e.param], ["req_42", "bad_size", "invalid_request_error", "size"]);
        return true;
      });
    });
  }
});

// ============================================================
// Вебхуки
// ============================================================

const referenceSecret = (apiKey) =>
  "whsec_" + createHmac("sha256", apiKey).update("nexus-webhook-signing-v1").digest("hex");

const BODY = JSON.stringify({ task_id: "t-1", status: "completed", result: { video_url: "u" } });

function signed(body, secret, ts = Math.floor(Date.now() / 1000)) {
  const sig = createHmac("sha256", secret).update(`${ts}.`).update(body).digest("hex");
  return { "X-Nexus-Timestamp": String(ts), "X-Nexus-Signature": `v1=${sig}` };
}

describe("webhooks", () => {
  const secret = referenceSecret("k-123");

  it("secret derivation matches the server", async () => {
    assert.equal(await webhookSecretFromApiKey("k-123"), secret);
  });

  it("unwraps a valid webhook from different header shapes and payload types", async () => {
    const h = signed(BODY, secret);
    const lower = Object.fromEntries(Object.entries(h).map(([k, v]) => [k.toLowerCase(), v]));
    for (const headers of [h, lower, new Headers(h), new Map(Object.entries(lower))]) {
      const event = await unwrapWebhook(BODY, headers, secret);
      assert.equal(event.task_id, "t-1");
    }
    const event = await unwrapWebhook(Buffer.from(BODY), { ...lower, "x-nexus-signature": [lower["x-nexus-signature"]] }, secret);
    assert.equal(event.status, "completed");
  });

  it("accepts any of several signatures", async () => {
    const h = signed(BODY, secret);
    h["X-Nexus-Signature"] = `v1=deadbeef, ${h["X-Nexus-Signature"]},v0=zzz`;
    await verifyWebhookSignature(BODY, h, secret);
  });

  for (const [name, mutate] of [
    ["tampered body", (h, b) => [h, `${b} `]],
    ["wrong signature", (h, b) => [{ ...h, "X-Nexus-Signature": `v1=${"0".repeat(64)}` }, b]],
    ["missing timestamp", (h, b) => [{ "X-Nexus-Signature": h["X-Nexus-Signature"] }, b]],
    ["bad timestamp", (h, b) => [{ ...h, "X-Nexus-Timestamp": "abc" }, b]],
    ["unknown version", (h, b) => [{ ...h, "X-Nexus-Signature": h["X-Nexus-Signature"].replace("v1=", "v2=") }, b]],
  ]) {
    it(`rejects: ${name}`, async () => {
      const [headers, body] = mutate(signed(BODY, secret), BODY);
      await assert.rejects(unwrapWebhook(body, headers, secret), WebhookVerificationError);
    });
  }

  it("rejects old timestamps unless tolerance is disabled", async () => {
    const old = signed(BODY, secret, Math.floor(Date.now() / 1000) - 3600);
    await assert.rejects(verifyWebhookSignature(BODY, old, secret), /tolerance/);
    await verifyWebhookSignature(BODY, old, secret, { toleranceSeconds: 0 });
  });

  it("rejects a non-JSON body", async () => {
    await assert.rejects(unwrapWebhook("not json", signed("not json", secret), secret), /JSON/);
  });

  it("client.webhooks uses the key's secret and fetches it from the API", async () => {
    const { fetchMock, calls } = mockFetch(() => json(200, { secret, algorithm: "HMAC-SHA256" }));
    const c = client(fetchMock);
    assert.equal(await c.webhooks.secret(), secret);
    assert.equal(calls[0].url, "https://api.test/webhooks/secret");
    assert.equal((await c.webhooks.unwrap(BODY, signed(BODY, secret))).task_id, "t-1");
    await assert.rejects(c.webhooks.unwrap(BODY, signed(BODY, referenceSecret("other"))), WebhookVerificationError);
  });
});

// ============================================================
// Исправления по ревью 0.4.0
// ============================================================

describe("review fixes", () => {
  it("Idempotency-Key from per-call headers is used (any case); one Authorization", async () => {
    const { fetchMock, calls } = mockFetch(() => json(202, { task_id: "t" }));
    const c = client(fetchMock, { defaultHeaders: { authorization: "Bearer proxy-token", "user-agent": "custom/1" } });
    await c.videos.create({ model: "m", prompt: "p" }, { headers: { "idempotency-key": "order-12345" } });
    const headers = calls[0].headers;
    const names = Object.keys(headers).map((k) => k.toLowerCase());
    assert.equal(names.filter((n) => n === "authorization").length, 1);
    assert.equal(names.filter((n) => n === "idempotency-key").length, 1);
    assert.equal(headers.Authorization, "Bearer k-123");
    assert.equal(headers["Idempotency-Key"], "order-12345");
    const ua = Object.entries(headers).filter(([k]) => k.toLowerCase() === "user-agent");
    assert.deepEqual(ua.map(([, v]) => v), ["custom/1"]); // свой User-Agent можно, но один
  });

  it("invalid header values fail fast, without retries", async () => {
    const { fetchMock, calls } = mockFetch(() => json(200, TASK));
    await assert.rejects(client(fetchMock, { maxRetries: 3, defaultHeaders: { "X-Team": "медиа" } }).tasks.get("t"), TypeError);
    assert.equal(calls.length, 0);
  });

  it("empty NEXUSAPI_BASE_URL is ignored", async () => {
    const saved = process.env.NEXUSAPI_BASE_URL;
    process.env.NEXUSAPI_BASE_URL = "";
    try {
      const { fetchMock, calls } = mockFetch(() => json(200, TASK));
      await new NexusClient({ apiKey: "k", fetch: fetchMock, maxRetries: 0 }).tasks.get("t");
      assert.equal(calls[0].url, "https://nexusapi.dev/tasks/t");
    } finally {
      if (saved === undefined) delete process.env.NEXUSAPI_BASE_URL;
      else process.env.NEXUSAPI_BASE_URL = saved;
    }
  });

  it("explicit per-call timeoutMs bounds the whole call including retries", { timeout: 5_000 }, async () => {
    const hanging = (url, init) =>
      new Promise((_, reject) => init.signal.addEventListener("abort", () => reject(init.signal.reason ?? new Error("aborted"))));
    let calls = 0;
    const counting = (url, init) => {
      calls += 1;
      return hanging(url, init);
    };
    const started = Date.now();
    await assert.rejects(
      client(counting, { maxRetries: 5 }).chat.completions.create({ model: "m", messages: [] }, { timeoutMs: 150 }),
      APITimeoutError,
    );
    assert.ok(Date.now() - started < 1_000);
    assert.equal(calls, 1);
  });

  it("v1.images: 500 (failed task) is not retried", async () => {
    const { fetchMock, calls } = mockFetch(() => json(500, { error: { message: "Task failed" } }, { "retry-after": "0" }));
    await assert.rejects(client(fetchMock, { maxRetries: 3 }).v1.images.generate({ model: "m", prompt: "p" }), InternalServerError);
    assert.equal(calls.length, 1);
  });

  it("undici header timeout maps to APITimeoutError", async () => {
    const { fetchMock } = mockFetch(() => {
      throw new TypeError("fetch failed", { cause: { code: "UND_ERR_HEADERS_TIMEOUT" } });
    });
    await assert.rejects(client(fetchMock).tasks.get("t"), APITimeoutError);
  });

  it("webhooks: parsed JSON payload and bad tolerance are rejected clearly", async () => {
    const secret = referenceSecret("k-123");
    await assert.rejects(unwrapWebhook(JSON.parse(BODY), signed(BODY, secret), secret), TypeError);
    await assert.rejects(verifyWebhookSignature(BODY, signed(BODY, secret), secret, { toleranceSeconds: Number.NaN }), RangeError);
  });

  it("v1.videos.create does not send timeoutMs in the body", async () => {
    const calls = [];
    const fetchMock = async (url, init) => {
      calls.push(JSON.parse(init.body));
      return json(200, { id: "v", status: "queued" });
    };
    await client(fetchMock).v1.videos.create({ model: "m", prompt: "p", timeoutMs: 5_000 });
    assert.equal("timeoutMs" in calls[0], false);
  });
});
