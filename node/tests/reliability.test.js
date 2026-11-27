/**
 * 0.3.1: надёжность — Retry-After, 409 после собственного повтора, таймауты,
 * ожидание задач, нестандартные ответы, проверка входных данных, утечка ключа.
 */
import { describe, it } from "node:test";
import assert from "node:assert/strict";
import { inspect } from "node:util";
import {
  APIResponseValidationError,
  ConflictError,
  NetworkError,
  NexusClient,
  NexusError,
  NotFoundError,
  TaskTimeoutError,
  ValidationError,
} from "../dist/index.js";
import { parseRetryAfterMs } from "../dist/internal.js";

function mockFetch(responder) {
  const calls = [];
  const fetchMock = async (url, init) => {
    const call = {
      url: String(url),
      method: init?.method ?? "GET",
      headers: init?.headers ?? {},
      body: init?.body ? JSON.parse(init.body) : undefined,
      signal: init?.signal,
    };
    calls.push(call);
    return responder(call, calls.length);
  };
  return { fetchMock, calls };
}

const json = (status, body, headers) =>
  new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json", ...(headers ?? {}) } });

const client = (fetchMock, extra = {}) =>
  new NexusClient({ apiKey: "secret-key-123", baseUrl: "https://api.test", maxRetries: 0, fetch: fetchMock, ...extra });

const abortError = () => new DOMException("This operation was aborted", "AbortError");

/** fetch, который честно реагирует на signal (как настоящий). */
function hangingFetch() {
  return (url, init) =>
    new Promise((_, reject) => {
      init.signal.addEventListener("abort", () => reject(init.signal.reason ?? abortError()));
    });
}

// ============================================================
// Retry-After
// ============================================================

describe("Retry-After", () => {
  const h = (obj) => new Headers(obj);
  it("parses seconds, ms and HTTP-date; rejects bad values", () => {
    assert.equal(parseRetryAfterMs(h({ "retry-after": "2" })), 2000);
    assert.equal(parseRetryAfterMs(h({ "retry-after": "0" })), 0);
    assert.equal(parseRetryAfterMs(h({ "retry-after-ms": "1500" })), 1500);
    assert.equal(parseRetryAfterMs(h({ "retry-after-ms": "250", "retry-after": "9" })), 250);
    const date = new Date(Date.now() + 10_000).toUTCString();
    const fromDate = parseRetryAfterMs(h({ "retry-after": date }));
    assert.ok(fromDate > 5_000 && fromDate <= 10_000);
    for (const bad of ["-1", "NaN", "Infinity", "3600", "soon", ""]) {
      assert.equal(parseRetryAfterMs(h({ "retry-after": bad })), undefined, bad);
    }
    assert.equal(parseRetryAfterMs(h({})), undefined);
  });

  it("an hour-long Retry-After does not stall the call", { timeout: 5_000 }, async () => {
    const { fetchMock, calls } = mockFetch((_, n) =>
      n === 1 ? json(503, { detail: "busy" }, { "retry-after": "3600" }) : json(202, { task_id: "t-1" }),
    );
    const task = await client(fetchMock, { maxRetries: 1 }).videos.create({ model: "m", prompt: "p" });
    assert.equal(task.id, "t-1");
    assert.equal(calls.length, 2);
  });

  it("abort during a retry pause rejects right away", { timeout: 5_000 }, async () => {
    const { fetchMock } = mockFetch(() => json(503, { error: { message: "busy" } }, { "retry-after": "30" }));
    const ac = new AbortController();
    const started = Date.now();
    setTimeout(() => ac.abort(), 50);
    await assert.rejects(
      client(fetchMock, { maxRetries: 2 }).chat.completions.create({ model: "m", messages: [], signal: ac.signal }),
      (e) => e.name === "AbortError",
    );
    assert.ok(Date.now() - started < 2_000);
  });
});

// ============================================================
// 409 после собственного повтора
// ============================================================

describe("409 after the SDK's own retry", () => {
  it("waits for the stored response of the first request", { timeout: 10_000 }, async () => {
    const { fetchMock, calls } = mockFetch((_, n) => {
      if (n === 1) throw new TypeError("fetch failed", { cause: { code: "ECONNRESET" } });
      if (n === 2) return json(409, { detail: "still being processed" });
      return json(202, { task_id: "t-1" });
    });
    const task = await client(fetchMock, { maxRetries: 1 }).videos.create({ model: "m", prompt: "p" });
    assert.equal(task.id, "t-1");
    assert.equal(new Set(calls.map((c) => c.headers["Idempotency-Key"])).size, 1);
    assert.equal(calls.length, 3);
  });

  it("raises ConflictError when the very first attempt gets 409", async () => {
    const { fetchMock, calls } = mockFetch(() => json(409, { detail: "still being processed" }));
    await assert.rejects(
      client(fetchMock, { maxRetries: 2 }).videos.create({ model: "m", prompt: "p", idempotencyKey: "order-12345" }),
      (e) => e instanceof ConflictError && e.idempotencyKey === "order-12345",
    );
    assert.equal(calls.length, 1);
  });
});

// ============================================================
// Таймауты
// ============================================================

describe("timeouts", () => {
  it("timeoutMs covers a body that stalls after the headers", { timeout: 5_000 }, async () => {
    const fetchMock = async (url, init) => {
      const body = new ReadableStream({
        start(controller) {
          controller.enqueue(new TextEncoder().encode('{"task_id":'));
          init.signal.addEventListener("abort", () => controller.error(init.signal.reason ?? abortError()));
        },
      });
      return new Response(body, { status: 200 });
    };
    const started = Date.now();
    await assert.rejects(client(fetchMock, { timeoutMs: 100 }).tasks.get("t-1"), (e) => {
      assert.ok(e instanceof NetworkError);
      assert.match(e.message, /timed out after 100ms/);
      assert.ok(e.cause);
      return true;
    });
    assert.ok(Date.now() - started < 2_000);
  });

  it("POST /generate waits at least 120 s, chat at least 600 s", () => {
    const c = client(async () => json(200, {}));
    assert.equal(c._generateTimeoutMs(), 120_000);
    assert.equal(c._longTimeoutMs(), 600_000);
  });

  it("rejects invalid timeoutMs / maxRetries / baseUrl", () => {
    assert.throws(() => new NexusClient({ apiKey: "k", timeoutMs: Infinity }), RangeError);
    assert.throws(() => new NexusClient({ apiKey: "k", timeoutMs: 0 }), RangeError);
    assert.throws(() => new NexusClient({ apiKey: "k", maxRetries: 1.5 }), /maxRetries/);
    assert.throws(() => new NexusClient({ apiKey: "k", baseUrl: "nexusapi.dev" }), TypeError);
  });

  it("baseUrl ending with /v1 is fixed", async () => {
    const { fetchMock, calls } = mockFetch(() => json(200, { object: "list", data: [] }));
    const c = new NexusClient({ apiKey: "k", baseUrl: "https://api.test/v1/", fetch: fetchMock, maxRetries: 0 });
    await c.v1.models.list();
    assert.equal(calls[0].url, "https://api.test/v1/models");
  });
});

// ============================================================
// Ожидание задач
// ============================================================

describe("Task.wait", () => {
  it("survives transient poll errors", async () => {
    let polls = 0;
    const { fetchMock } = mockFetch((call) => {
      if (call.method === "POST") return json(202, { task_id: "t-1" });
      polls += 1;
      if (polls <= 2) return new Response("Bad Gateway", { status: 502 });
      if (polls === 3) throw new TypeError("fetch failed", { cause: { code: "ECONNRESET" } });
      return json(200, { task_id: "t-1", status: "completed", result: { video_url: "https://s3/v.mp4" } });
    });
    const task = await client(fetchMock).videos.create({ model: "m", prompt: "p" });
    await task.wait({ timeoutMs: 10_000, pollIntervalMs: 1 });
    assert.equal(task.videoUrl, "https://s3/v.mp4");
  });

  it("timeout keeps the task so it can be awaited again", async () => {
    const { fetchMock } = mockFetch((call) =>
      call.method === "POST" ? json(202, { task_id: "t-1" }) : json(200, { task_id: "t-1", status: "processing" }),
    );
    await assert.rejects(
      client(fetchMock).images.generate({ model: "seedream-5.0-pro", prompt: "p", timeout: 30, pollInterval: 5 }),
      (e) => e instanceof TaskTimeoutError && e.taskId === "t-1" && e.task.id === "t-1" && /still running/.test(e.message),
    );
  });

  it("permanent errors are not swallowed and carry taskId", async () => {
    const { fetchMock } = mockFetch((call) =>
      call.method === "POST" ? json(202, { task_id: "t-1" }) : json(404, { detail: "Task not found" }),
    );
    const task = await client(fetchMock).videos.create({ model: "m", prompt: "p" });
    await assert.rejects(task.wait(10_000, 1), (e) => e instanceof NotFoundError && e.taskId === "t-1");
  });

  it("wait can be cancelled with a signal", { timeout: 5_000 }, async () => {
    const { fetchMock } = mockFetch((call) =>
      call.method === "POST" ? json(202, { task_id: "t-1" }) : json(200, { task_id: "t-1", status: "processing" }),
    );
    const task = await client(fetchMock).videos.create({ model: "m", prompt: "p" });
    const ac = new AbortController();
    setTimeout(() => ac.abort(), 50);
    await assert.rejects(task.wait({ timeoutMs: 60_000, pollIntervalMs: 1_000, signal: ac.signal }), (e) => e.name === "AbortError");
  });

  it("v1.videos.wait survives transient errors", async () => {
    let polls = 0;
    const { fetchMock } = mockFetch(() => {
      polls += 1;
      if (polls === 1) return json(503, { error: { message: "busy" } });
      return json(200, { id: "v-1", status: "completed", url: "u" });
    });
    const done = await client(fetchMock).v1.videos.wait("v-1", { pollIntervalMs: 1 });
    assert.equal(done.url, "u");
  });

  it("task properties tolerate odd results; audio urls", () => {
    // Task создаётся через tasks.get, проверяем через мок.
    return (async () => {
      const { fetchMock } = mockFetch((_, n) =>
        n === 1
          ? json(200, { task_id: "t", status: "completed", result: ["unexpected"] })
          : json(200, { task_id: "t", status: "completed", result: { audio_urls: ["a.mp3", "b.mp3"] } }),
      );
      const c = client(fetchMock);
      const odd = await c.tasks.get("t");
      assert.equal(odd.videoUrl, undefined);
      assert.deepEqual(odd.imageUrls, []);
      const audio = await c.tasks.get("t");
      assert.equal(audio.audioUrl, "a.mp3");
      assert.deepEqual(audio.audioUrls, ["a.mp3", "b.mp3"]);
    })();
  });
});

// ============================================================
// Нестандартные ответы
// ============================================================

describe("odd responses", () => {
  it("non-JSON 2xx → APIResponseValidationError", async () => {
    const { fetchMock } = mockFetch(() => new Response("<html>maintenance</html>", { status: 200 }));
    await assert.rejects(
      client(fetchMock).tasks.get("t-1"),
      (e) => e instanceof APIResponseValidationError && e.status === 200 && e.responseBody.includes("maintenance"),
    );
  });

  it("202 without task_id → APIResponseValidationError with the key", async () => {
    const { fetchMock } = mockFetch(() => json(202, { message: "accepted" }));
    await assert.rejects(
      client(fetchMock).videos.create({ model: "m", prompt: "p", idempotencyKey: "order-12345" }),
      (e) => e instanceof APIResponseValidationError && e.idempotencyKey === "order-12345",
    );
  });

  for (const [body, expected] of [
    [{ detail: ["just a string"] }, "just a string"],
    [{ error: "plain error" }, "plain error"],
    [{ detail: { message: "nested" } }, "nested"],
    [{ detail: [{ loc: ["body", "params", "duration"], msg: "too long" }] }, "params.duration: too long"],
  ]) {
    it(`error message from ${JSON.stringify(body)}`, async () => {
      const { fetchMock } = mockFetch(() => json(400, body));
      await assert.rejects(client(fetchMock).tasks.get("t-1"), (e) => e instanceof ValidationError && e.message === expected);
    });
  }

  it("long HTML error is truncated; headers are kept", async () => {
    const { fetchMock } = mockFetch(
      () => new Response(`<html>${"x".repeat(5000)}</html>`, { status: 502, headers: { "x-trace": "abc" } }),
    );
    await assert.rejects(client(fetchMock).tasks.get("t-1"), (e) => {
      assert.ok(e instanceof NexusError);
      assert.ok(e.message.length <= 501);
      assert.equal(e.headers["x-trace"], "abc");
      return true;
    });
  });
});

// ============================================================
// Входные данные и утечки
// ============================================================

describe("input checks and secret hygiene", () => {
  it("task id is escaped", async () => {
    const { fetchMock, calls } = mockFetch(() => json(200, { task_id: "x", status: "pending" }));
    await client(fetchMock).tasks.get("../me/keys?x=1");
    assert.equal(calls[0].url, "https://api.test/tasks/..%2Fme%2Fkeys%3Fx%3D1");
  });

  it("idempotency key is checked before sending", async () => {
    const { fetchMock, calls } = mockFetch(() => json(202, { task_id: "t" }));
    const c = client(fetchMock);
    await assert.rejects(c.videos.create({ model: "m", prompt: "p", idempotencyKey: "short" }), RangeError);
    await assert.rejects(c.videos.create({ model: "m", prompt: "p", idempotencyKey: "ключ-заказа-1" }), RangeError);
    await assert.rejects(
      c.chat.completions.create({ model: "m", messages: [], stream: true, idempotencyKey: "x".repeat(300) }),
      RangeError,
    );
    assert.equal(calls.length, 0);
  });

  it("bad request data is not retried as a network error", async () => {
    const { fetchMock, calls } = mockFetch(() => json(202, { task_id: "t" }));
    await assert.rejects(client(fetchMock, { maxRetries: 3 }).videos.create({ model: "m", prompt: "p", n: 10n }), TypeError);
    assert.equal(calls.length, 0);
  });

  it("signal is not sent in the request body", async () => {
    const { fetchMock, calls } = mockFetch(() => json(200, { created: 1, data: [{ url: "u" }] }));
    await client(fetchMock).v1.images.generate({ model: "m", prompt: "p", signal: new AbortController().signal });
    assert.equal("signal" in calls[0].body, false);
  });

  it("API key does not leak through inspect or JSON", async () => {
    const { fetchMock } = mockFetch((call) =>
      call.method === "POST" ? json(202, { task_id: "t-1" }) : json(200, { task_id: "t-1", status: "pending" }),
    );
    const c = client(fetchMock);
    const task = await c.videos.create({ model: "m", prompt: "p" });
    assert.equal(inspect(c, { depth: 5 }).includes("secret-key-123"), false);
    assert.equal(inspect(task, { depth: 5 }).includes("secret-key-123"), false);
    const parsed = JSON.parse(JSON.stringify(task));
    assert.deepEqual(Object.keys(parsed).sort(), ["createdAt", "error", "id", "model", "result", "status"].filter((k) => k in parsed).sort());
    assert.equal(parsed.id, "t-1");
  });

  it("stream network error does not promise a replay", async () => {
    const fetchMock = hangingFetch();
    await assert.rejects(
      client(fetchMock, { timeoutMs: 50 }).chat.completions.create({
        model: "m",
        messages: [],
        stream: true,
        idempotencyKey: "stream-key-1",
        timeoutMs: 50,
      }),
      (e) => e instanceof NetworkError && /cannot be replayed/.test(e.message) && !/stored result/.test(e.message),
    );
  });
});
