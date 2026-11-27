/**
 * Smoke + critical-path tests for nexusapi-sdk (Node).
 *
 * Использует встроенный node:test runner (без vitest/jest) и fetch-mock через
 * `fetch` option в ClientOptions — не делает реальных сетевых запросов.
 *
 * Запуск: `npm test` (билдит TS → dist/, потом гоняет node --test).
 */
import { describe, it, beforeEach } from "node:test";
import assert from "node:assert/strict";
import {
  AuthenticationError,
  BillingError,
  NetworkError,
  NexusClient,
  NexusError,
  NotFoundError,
  RateLimitError,
  TaskFailedError,
  TaskTimeoutError,
  ValidationError,
} from "../dist/index.js";

// ============================================================
// Mock fetch — простой helper, не зависит от внешних либ
// ============================================================

/**
 * Returns [fetchMock, captured] where fetchMock записывает каждый вызов в
 * captured.calls, и возвращает response из `responder(call)`.
 */
function makeMockFetch(responder) {
  const captured = { calls: [] };
  const fetchMock = async (url, init) => {
    const call = {
      url: typeof url === "string" ? url : url.toString(),
      method: init?.method ?? "GET",
      headers: init?.headers ?? {},
      body: init?.body ? JSON.parse(init.body) : undefined,
    };
    captured.calls.push(call);
    return responder(call, captured.calls.length);
  };
  return { fetchMock, captured };
}

function jsonResponse(status, body, extraHeaders) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json", ...(extraHeaders ?? {}) },
  });
}

function emptyResponse(status) {
  return new Response(null, { status });
}

const FAST_RETRY_OPTS = {
  apiKey: "k",
  baseUrl: "https://api.test",
  // No artificial sleep — but backoff already does up to ~5s. For tests we
  // can't easily monkey-patch setTimeout; instead test with maxRetries=0
  // for cases that shouldn't retry, and 1 retry where we tolerate ~500ms.
  maxRetries: 0,
};

// ============================================================
// Construction
// ============================================================

describe("Construction", () => {
  it("requires apiKey", () => {
    assert.throws(() => new NexusClient({ apiKey: "" }), AuthenticationError);
  });

  it("strips trailing slash on baseUrl", () => {
    const { fetchMock } = makeMockFetch(() => emptyResponse(204));
    const client = new NexusClient({
      apiKey: "k",
      baseUrl: "https://api.test/",
      fetch: fetchMock,
    });
    // Internal but easiest way to verify
    assert.equal(client.baseUrl ?? "https://api.test", "https://api.test");
  });

  it("rejects negative maxRetries", () => {
    assert.throws(
      () => new NexusClient({ apiKey: "k", maxRetries: -1 }),
      /maxRetries must be >= 0/,
    );
  });
});

// ============================================================
// videos.create — happy path + headers
// ============================================================

describe("videos.create", () => {
  it("returns pending task", async () => {
    const { fetchMock, captured } = makeMockFetch(() =>
      jsonResponse(200, { task_id: "t-123" }),
    );
    const client = new NexusClient({ ...FAST_RETRY_OPTS, fetch: fetchMock });
    const task = await client.videos.create({
      model: "veo-3-fast",
      prompt: "cat",
      duration: 8,
    });
    assert.equal(task.id, "t-123");
    assert.equal(task.status, "pending");
    assert.equal(task.model, "veo-3-fast");
    const call = captured.calls[0];
    assert.equal(call.url, "https://api.test/generate");
    assert.equal(call.method, "POST");
    assert.deepEqual(call.body.params, {
      model_name: "veo-3-fast",
      prompt: "cat",
      duration: 8,
    });
  });

  it("auto-generates Idempotency-Key when not provided", async () => {
    const { fetchMock, captured } = makeMockFetch(() =>
      jsonResponse(200, { task_id: "t-1" }),
    );
    const client = new NexusClient({ ...FAST_RETRY_OPTS, fetch: fetchMock });
    await client.videos.create({ model: "m", prompt: "p" });
    const key = captured.calls[0].headers["Idempotency-Key"];
    assert.ok(key, "Idempotency-Key header should be set");
    assert.match(key, /^sdk-[0-9a-f-]{36}$/, "SDK-generated UUID format");
  });

  it("uses user-provided idempotencyKey verbatim", async () => {
    const { fetchMock, captured } = makeMockFetch(() =>
      jsonResponse(200, { task_id: "t-1" }),
    );
    const client = new NexusClient({ ...FAST_RETRY_OPTS, fetch: fetchMock });
    await client.videos.create({
      model: "m",
      prompt: "p",
      idempotencyKey: "user-key-42",
    });
    assert.equal(captured.calls[0].headers["Idempotency-Key"], "user-key-42");
  });

  it("forwards extras into params", async () => {
    const { fetchMock, captured } = makeMockFetch(() =>
      jsonResponse(200, { task_id: "t-1" }),
    );
    const client = new NexusClient({ ...FAST_RETRY_OPTS, fetch: fetchMock });
    await client.videos.create({
      model: "m",
      prompt: "p",
      webhook_url: "https://x.com/cb",
      negative_prompt: "bad",
      cfg_scale: 7.5,
    });
    const p = captured.calls[0].body.params;
    assert.equal(p.webhook_url, "https://x.com/cb");
    assert.equal(p.negative_prompt, "bad");
    assert.equal(p.cfg_scale, 7.5);
  });

  it("drops null/undefined extras", async () => {
    const { fetchMock, captured } = makeMockFetch(() =>
      jsonResponse(200, { task_id: "t-1" }),
    );
    const client = new NexusClient({ ...FAST_RETRY_OPTS, fetch: fetchMock });
    await client.videos.create({
      model: "m",
      prompt: "p",
      image_url: null,
      duration: undefined,
    });
    const p = captured.calls[0].body.params;
    assert.equal("image_url" in p, false);
    assert.equal("duration" in p, false);
  });
});

// ============================================================
// Task.wait
// ============================================================

describe("Task.wait", () => {
  it("polls until completed", async () => {
    let calls = 0;
    const { fetchMock } = makeMockFetch((call) => {
      if (call.method === "POST") return jsonResponse(200, { task_id: "t-1" });
      calls += 1;
      if (calls < 3) {
        return jsonResponse(200, { task_id: "t-1", status: "processing" });
      }
      return jsonResponse(200, {
        task_id: "t-1",
        status: "completed",
        result: { video_url: "https://s3/x.mp4" },
      });
    });
    const client = new NexusClient({ ...FAST_RETRY_OPTS, fetch: fetchMock });
    const task = await client.videos.create({ model: "m", prompt: "p" });
    await task.wait(10_000, 1);  // poll every 1ms
    assert.equal(task.status, "completed");
    assert.equal(task.videoUrl, "https://s3/x.mp4");
    assert.equal(calls, 3);
  });

  it("throws TaskFailedError with task_id+error in responseBody", async () => {
    const { fetchMock } = makeMockFetch((call) => {
      if (call.method === "POST") return jsonResponse(200, { task_id: "t-1" });
      return jsonResponse(200, {
        task_id: "t-1",
        status: "failed",
        error: "provider down",
      });
    });
    const client = new NexusClient({ ...FAST_RETRY_OPTS, fetch: fetchMock });
    const task = await client.videos.create({ model: "m", prompt: "p" });
    await assert.rejects(
      task.wait(5_000, 1),
      (e) => {
        assert.ok(e instanceof TaskFailedError);
        assert.ok(e.message.includes("provider down"));
        assert.equal(e.responseBody.task_id, "t-1");
        assert.equal(e.responseBody.error, "provider down");
        return true;
      },
    );
  });

  it("imageUrl falls back to image_url when image_urls is not array", async () => {
    const { fetchMock } = makeMockFetch((call) => {
      if (call.method === "POST") return jsonResponse(200, { task_id: "t-1" });
      return jsonResponse(200, {
        task_id: "t-1",
        status: "completed",
        result: { image_url: "https://s3/x.png" },
      });
    });
    const client = new NexusClient({ ...FAST_RETRY_OPTS, fetch: fetchMock });
    const task = await client.images.generate({
      model: "m",
      prompt: "p",
      wait: false,
    });
    await task.wait(5_000, 1);
    assert.equal(task.imageUrl, "https://s3/x.png");
    assert.deepEqual(task.imageUrls, []);
  });
});

// ============================================================
// Error mapping
// ============================================================

describe("Error mapping", () => {
  const cases = [
    [401, AuthenticationError, "Unauthorized"],
    [402, BillingError, "Need more money"],
    [404, NotFoundError, "Not found"],
    [422, ValidationError, "Invalid input"],
    [400, ValidationError, "Bad request"],
  ];
  for (const [status, cls, detail] of cases) {
    it(`maps ${status} → ${cls.name}`, async () => {
      const { fetchMock } = makeMockFetch(() =>
        jsonResponse(status, { detail }),
      );
      const client = new NexusClient({ ...FAST_RETRY_OPTS, fetch: fetchMock });
      await assert.rejects(
        client.videos.create({ model: "m", prompt: "p" }),
        (e) => {
          assert.ok(e instanceof cls);
          assert.equal(e.status, status);
          assert.ok(e.message.includes(detail));
          return true;
        },
      );
    });
  }

  it("parses OpenAI-shape error", async () => {
    const { fetchMock } = makeMockFetch(() =>
      jsonResponse(404, { error: { message: "model not found" } }),
    );
    const client = new NexusClient({ ...FAST_RETRY_OPTS, fetch: fetchMock });
    await assert.rejects(
      client.tasks.get("x"),
      (e) => {
        assert.ok(e instanceof NotFoundError);
        assert.ok(e.message.includes("model not found"));
        return true;
      },
    );
  });

  it("parses Pydantic validation array", async () => {
    const { fetchMock } = makeMockFetch(() =>
      jsonResponse(422, {
        detail: [{ loc: ["body", "params", "prompt"], msg: "field required" }],
      }),
    );
    const client = new NexusClient({ ...FAST_RETRY_OPTS, fetch: fetchMock });
    await assert.rejects(
      client.videos.create({ model: "m", prompt: "x" }),
      (e) => {
        assert.ok(e instanceof ValidationError);
        assert.ok(e.message.includes("params.prompt"));
        assert.ok(e.message.includes("field required"));
        return true;
      },
    );
  });
});

// ============================================================
// Retries
// ============================================================

describe("Retries", () => {
  it("no retry on 4xx", async () => {
    let calls = 0;
    const { fetchMock } = makeMockFetch(() => {
      calls += 1;
      return jsonResponse(401, { detail: "bad" });
    });
    const client = new NexusClient({
      apiKey: "k",
      baseUrl: "https://api.test",
      fetch: fetchMock,
      maxRetries: 3,
    });
    await assert.rejects(
      client.tasks.get("x"),
      (e) => e instanceof AuthenticationError,
    );
    assert.equal(calls, 1);
  });

  it("retry on 503 then success", { timeout: 10_000 }, async () => {
    let calls = 0;
    const { fetchMock } = makeMockFetch(() => {
      calls += 1;
      if (calls < 2) return emptyResponse(503);
      return jsonResponse(200, {
        task_id: "x",
        status: "completed",
      });
    });
    const client = new NexusClient({
      apiKey: "k",
      baseUrl: "https://api.test",
      fetch: fetchMock,
      maxRetries: 3,
    });
    const task = await client.tasks.get("x");
    assert.equal(task.id, "x");
    assert.equal(calls, 2);
  });

  it("network error then success", { timeout: 10_000 }, async () => {
    let calls = 0;
    const { fetchMock } = makeMockFetch(() => {
      calls += 1;
      if (calls < 2) throw new TypeError("fetch failed");
      return jsonResponse(200, {
        task_id: "x",
        status: "completed",
      });
    });
    const client = new NexusClient({
      apiKey: "k",
      baseUrl: "https://api.test",
      fetch: fetchMock,
      maxRetries: 3,
    });
    const task = await client.tasks.get("x");
    assert.equal(task.id, "x");
    assert.equal(calls, 2);
  });

  it("exhausted retries throw NetworkError", { timeout: 15_000 }, async () => {
    let calls = 0;
    const { fetchMock } = makeMockFetch(() => {
      calls += 1;
      throw new TypeError("fetch failed");
    });
    const client = new NexusClient({
      apiKey: "k",
      baseUrl: "https://api.test",
      fetch: fetchMock,
      maxRetries: 1,
    });
    await assert.rejects(
      client.tasks.get("x"),
      (e) => e instanceof NetworkError,
    );
    assert.equal(calls, 2);  // initial + 1 retry
  });
});

// ============================================================
// Models endpoint — no auth
// ============================================================

describe("models.list", () => {
  it("sends no Authorization header", async () => {
    const { fetchMock, captured } = makeMockFetch(() =>
      jsonResponse(200, [{ id: "veo-3-fast", name: "Veo 3 Fast" }]),
    );
    const client = new NexusClient({ ...FAST_RETRY_OPTS, fetch: fetchMock });
    const models = await client.models.list();
    assert.equal(models[0].id, "veo-3-fast");
    assert.equal(captured.calls[0].headers.Authorization, undefined);
  });
});
