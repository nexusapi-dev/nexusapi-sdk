/**
 * Tests for the OpenAI-compatible /v1 resources (0.3.0): chat.completions
 * (обычный ответ и SSE-стрим), v1.models, v1.images, v1.videos и политика
 * повторов. Сеть не используется — fetch подменяется через ClientOptions.fetch.
 */
import { describe, it } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import {
  ConflictError,
  NetworkError,
  NexusClient,
  NexusError,
  NotFoundError,
  Stream,
  StreamError,
  TaskFailedError,
  TaskTimeoutError,
  ValidationError,
  VERSION,
} from "../dist/index.js";

const PKG = JSON.parse(readFileSync(new URL("../package.json", import.meta.url), "utf8"));

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

function json(status, body, headers) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json", ...(headers ?? {}) },
  });
}

function sse(events, { done = true, sep = "\n" } = {}) {
  let out = "";
  for (const e of events) out += `data: ${typeof e === "string" ? e : JSON.stringify(e)}${sep}${sep}`;
  if (done) out += `data: [DONE]${sep}${sep}`;
  return out;
}

/** Response, тело которого отдаётся кусками заданного размера (в байтах). */
function chunkedResponse(text, size, { onCancel } = {}) {
  const bytes = new TextEncoder().encode(text);
  let offset = 0;
  const body = new ReadableStream({
    pull(controller) {
      if (offset >= bytes.length) {
        controller.close();
        return;
      }
      controller.enqueue(bytes.slice(offset, offset + size));
      offset += size;
    },
    cancel() {
      onCancel?.();
    },
  });
  return new Response(body, { status: 200, headers: { "content-type": "text/event-stream" } });
}

const chunk = (content) => ({
  id: "c-1",
  object: "chat.completion.chunk",
  model: "claude-sonnet-5",
  choices: [{ index: 0, delta: { content }, finish_reason: null }],
});

const COMPLETION = {
  id: "c-1",
  object: "chat.completion",
  model: "claude-sonnet-5",
  choices: [{ index: 0, message: { role: "assistant", content: "Привет!" }, finish_reason: "stop" }],
  usage: { prompt_tokens: 5, completion_tokens: 3, total_tokens: 8 },
};

function client(fetchMock, extra = {}) {
  return new NexusClient({ apiKey: "k", baseUrl: "https://api.test", maxRetries: 0, fetch: fetchMock, ...extra });
}

async function collect(stream) {
  const out = [];
  for await (const c of stream) out.push(c);
  return out;
}

// ============================================================
// Общее
// ============================================================

describe("0.3.0 basics", () => {
  it("VERSION matches package.json and goes into User-Agent", async () => {
    assert.equal(VERSION, PKG.version);
    const { fetchMock, calls } = mockFetch(() => json(200, { object: "list", data: [] }));
    await client(fetchMock).v1.models.list();
    assert.equal(calls[0].headers["User-Agent"], `nexusapi-node/${PKG.version}`);
  });

  it("v1.chat is the same object as chat", () => {
    const c = client(async () => json(200, {}));
    assert.equal(c.v1.chat, c.chat);
  });

  it("long requests wait at least 600s unless overridden", () => {
    assert.equal(client(async () => json(200, {}))._longTimeoutMs(), 600_000);
    assert.equal(client(async () => json(200, {}), { timeoutMs: 900_000 })._longTimeoutMs(), 900_000);
    assert.equal(client(async () => json(200, {}))._longTimeoutMs(30_000), 30_000);
  });
});

// ============================================================
// chat.completions — без стрима
// ============================================================

describe("chat.completions.create", () => {
  it("posts OpenAI body with auto Idempotency-Key", async () => {
    const { fetchMock, calls } = mockFetch(() => json(200, COMPLETION));
    const reply = await client(fetchMock).chat.completions.create({
      model: "claude-sonnet-5",
      messages: [{ role: "user", content: "Привет" }],
      max_tokens: 100,
      temperature: undefined,
      tools: [{ type: "function", function: { name: "f" } }],
    });
    assert.deepEqual(reply, COMPLETION);
    const call = calls[0];
    assert.equal(call.url, "https://api.test/v1/chat/completions");
    assert.equal(call.method, "POST");
    assert.equal(call.headers.Authorization, "Bearer k");
    assert.match(call.headers["Idempotency-Key"], /^sdk-[0-9a-f-]{36}$/);
    assert.deepEqual(call.body, {
      model: "claude-sonnet-5",
      messages: [{ role: "user", content: "Привет" }],
      max_tokens: 100,
      tools: [{ type: "function", function: { name: "f" } }],
    });
  });

  it("uses the user idempotencyKey and keeps it across retries", async () => {
    const { fetchMock, calls } = mockFetch((_, n) =>
      n === 1 ? json(503, { error: { message: "busy" } }, { "retry-after": "0" }) : json(200, COMPLETION),
    );
    await client(fetchMock, { maxRetries: 2 }).chat.completions.create({
      model: "m",
      messages: [],
      idempotencyKey: "my-key-123",
    });
    assert.equal(calls.length, 2);
    assert.equal(calls[0].headers["Idempotency-Key"], "my-key-123");
    assert.equal(calls[1].headers["Idempotency-Key"], "my-key-123");
  });

  for (const status of [500, 502, 504]) {
    it(`does not retry ${status} (paid work may already run)`, async () => {
      const { fetchMock, calls } = mockFetch(() => json(status, { error: { message: "x" } }, { "retry-after": "0" }));
      await assert.rejects(
        client(fetchMock, { maxRetries: 3 }).chat.completions.create({ model: "m", messages: [] }),
        (e) => e instanceof NexusError && e.status === status,
      );
      assert.equal(calls.length, 1);
    });
  }

  it("maps OpenAI-shape 404 and 409", async () => {
    const notFound = mockFetch(() => json(404, { error: { message: "Model 'x' is not supported", type: "not_found_error" } }));
    await assert.rejects(
      client(notFound.fetchMock).chat.completions.create({ model: "x", messages: [] }),
      (e) => e instanceof NotFoundError && e.message.includes("not supported"),
    );
    const conflict = mockFetch(() => json(409, { detail: "still being processed" }));
    await assert.rejects(
      client(conflict.fetchMock).chat.completions.create({ model: "m", messages: [] }),
      ConflictError,
    );
  });

  it("user AbortSignal cancels without retry", async () => {
    const { fetchMock, calls } = mockFetch((call) => {
      if (call.signal?.aborted) throw new DOMException("aborted", "AbortError");
      return json(200, COMPLETION);
    });
    const ac = new AbortController();
    ac.abort();
    await assert.rejects(
      client(fetchMock, { maxRetries: 3 }).chat.completions.create({ model: "m", messages: [], signal: ac.signal }),
      (e) => e.name === "AbortError",
    );
    assert.equal(calls.length, 1);
  });
});

// ============================================================
// chat.completions — стрим
// ============================================================

describe("chat.completions.create({ stream: true })", () => {
  it("yields chunks; no auto Idempotency-Key for streams", async () => {
    const { fetchMock, calls } = mockFetch(
      () => new Response(": keep-alive\n\n" + sse([chunk("При"), chunk("вет")]), { status: 200 }),
    );
    const stream = await client(fetchMock).chat.completions.create({ model: "m", messages: [], stream: true });
    assert.ok(stream instanceof Stream);
    const parts = (await collect(stream)).map((c) => c.choices[0].delta.content);
    assert.equal(parts.join(""), "Привет");
    assert.equal(calls[0].body.stream, true);
    assert.equal(calls[0].headers.Accept, "text/event-stream");
    assert.equal("Idempotency-Key" in calls[0].headers, false);
  });

  it("sends an explicit idempotencyKey for streams", async () => {
    const { fetchMock, calls } = mockFetch(() => new Response(sse([]), { status: 200 }));
    const stream = await client(fetchMock).chat.completions.create({
      model: "m",
      messages: [],
      stream: true,
      idempotencyKey: "stream-key-1",
    });
    assert.deepEqual(await collect(stream), []);
    assert.equal(calls[0].headers["Idempotency-Key"], "stream-key-1");
  });

  it("handles CRLF, tiny chunks and UTF-8 split across chunks", async () => {
    const text = sse([chunk("Привет, "), chunk("мир")], { sep: "\r\n" });
    const { fetchMock } = mockFetch(() => chunkedResponse(text, 3));
    const stream = await client(fetchMock).chat.completions.create({ model: "m", messages: [], stream: true });
    const parts = (await collect(stream)).map((c) => c.choices[0].delta.content);
    assert.deepEqual(parts, ["Привет, ", "мир"]);
  });

  it("ends cleanly without [DONE]", async () => {
    const { fetchMock } = mockFetch(() => new Response('data: {"choices": []}', { status: 200 }));
    const stream = await client(fetchMock).chat.completions.create({ model: "m", messages: [], stream: true });
    assert.deepEqual(await collect(stream), [{ choices: [] }]);
  });

  it("throws StreamError on an error event inside the stream", async () => {
    const errorEvent = {
      error: {
        message: "Text provider could not complete this request. No funds were charged.",
        type: "upstream_error",
        code: "text_generation_failed",
      },
    };
    const { fetchMock } = mockFetch(() => new Response(sse([chunk("a"), errorEvent]), { status: 200 }));
    const stream = await client(fetchMock).chat.completions.create({ model: "m", messages: [], stream: true });
    const got = [];
    await assert.rejects(
      (async () => {
        for await (const c of stream) got.push(c);
      })(),
      (e) => e instanceof StreamError && e.responseBody.error.code === "text_generation_failed",
    );
    assert.equal(got.length, 1);
  });

  it("break closes the connection", async () => {
    let cancelled = false;
    const text = sse([chunk("a"), chunk("b"), chunk("c")]);
    const { fetchMock } = mockFetch(() => chunkedResponse(text, 10, { onCancel: () => (cancelled = true) }));
    const stream = await client(fetchMock).chat.completions.create({ model: "m", messages: [], stream: true });
    for await (const c of stream) {
      assert.equal(c.choices[0].delta.content, "a");
      break;
    }
    assert.equal(cancelled, true);
  });

  it("abort() ends iteration without error", async () => {
    const { fetchMock } = mockFetch((call) => {
      const signal = call.signal;
      const body = new ReadableStream({
        start(controller) {
          controller.enqueue(new TextEncoder().encode(sse([chunk("a")], { done: false })));
          signal.addEventListener("abort", () => controller.error(new DOMException("aborted", "AbortError")));
        },
      });
      return new Response(body, { status: 200 });
    });
    const stream = await client(fetchMock).chat.completions.create({ model: "m", messages: [], stream: true });
    const got = [];
    for await (const c of stream) {
      got.push(c);
      stream.abort();
    }
    assert.equal(got.length, 1);
  });

  it("mid-stream disconnect → NetworkError", async () => {
    const { fetchMock } = mockFetch(() => {
      let sent = false;
      const body = new ReadableStream({
        pull(controller) {
          if (!sent) {
            sent = true;
            controller.enqueue(new TextEncoder().encode(sse([chunk("a")], { done: false })));
          } else {
            controller.error(new TypeError("terminated"));
          }
        },
      });
      return new Response(body, { status: 200 });
    });
    const stream = await client(fetchMock).chat.completions.create({ model: "m", messages: [], stream: true });
    await assert.rejects(collect(stream), NetworkError);
  });

  it("HTTP error before the stream starts", async () => {
    const { fetchMock } = mockFetch(() => json(400, { error: { message: "bad sampling" } }));
    await assert.rejects(
      client(fetchMock).chat.completions.create({ model: "m", messages: [], stream: true }),
      (e) => e instanceof ValidationError && e.message.includes("bad sampling"),
    );
  });

  it("retries only requests that never reached the server", { timeout: 10_000 }, async () => {
    const refused = mockFetch((_, n) => {
      if (n === 1) throw new TypeError("fetch failed", { cause: { code: "ECONNREFUSED" } });
      return new Response(sse([chunk("ok")]), { status: 200 });
    });
    const s1 = await client(refused.fetchMock, { maxRetries: 1 }).chat.completions.create({
      model: "m",
      messages: [],
      stream: true,
    });
    assert.deepEqual((await collect(s1)).map((c) => c.choices[0].delta.content), ["ok"]);
    assert.equal(refused.calls.length, 2);

    const reset = mockFetch(() => {
      throw new TypeError("fetch failed", { cause: { code: "ECONNRESET" } });
    });
    await assert.rejects(
      client(reset.fetchMock, { maxRetries: 1 }).chat.completions.create({ model: "m", messages: [], stream: true }),
      NetworkError,
    );
    assert.equal(reset.calls.length, 1);
  });
});

// ============================================================
// v1.models / v1.images / v1.videos
// ============================================================

describe("v1 resources", () => {
  it("v1.models.list sends the key and returns data", async () => {
    const { fetchMock, calls } = mockFetch(() =>
      json(200, { object: "list", data: [{ id: "veo-3-fast", object: "model", created: 1, owned_by: "nexus" }] }),
    );
    const models = await client(fetchMock).v1.models.list();
    assert.deepEqual(models.map((m) => m.id), ["veo-3-fast"]);
    assert.equal(calls[0].url, "https://api.test/v1/models");
    assert.equal(calls[0].headers.Authorization, "Bearer k");
  });

  it("v1.images.generate", async () => {
    const { fetchMock, calls } = mockFetch(() => json(200, { created: 1, data: [{ url: "https://s3/x.png" }] }));
    const resp = await client(fetchMock).v1.images.generate({
      model: "nano-banana",
      prompt: "cat",
      size: "1024x1024",
      quality: undefined,
      image_url: "https://example.com/in.png",
    });
    assert.equal(resp.data[0].url, "https://s3/x.png");
    assert.equal(calls[0].url, "https://api.test/v1/images/generations");
    assert.deepEqual(calls[0].body, {
      model: "nano-banana",
      prompt: "cat",
      size: "1024x1024",
      image_url: "https://example.com/in.png",
    });
    assert.match(calls[0].headers["Idempotency-Key"], /^sdk-/);
  });

  it("v1.images.generate does not retry 504 (task still running)", async () => {
    const { fetchMock, calls } = mockFetch(() =>
      json(504, { error: { message: "Task did not complete within 180s", type: "timeout_error" } }, { "retry-after": "0" }),
    );
    await assert.rejects(
      client(fetchMock, { maxRetries: 3 }).v1.images.generate({ model: "m", prompt: "p" }),
      (e) => e.status === 504,
    );
    assert.equal(calls.length, 1);
  });

  it("v1.videos.create → wait → completed", async () => {
    let polls = 0;
    const { fetchMock, calls } = mockFetch((call) => {
      if (call.method === "POST") {
        return json(200, { id: "v-1", object: "video", model: "veo-3-fast", status: "queued", progress: 0, created_at: 1 });
      }
      polls += 1;
      if (polls < 3) return json(200, { id: "v-1", status: "in_progress", progress: 50 });
      return json(200, { id: "v-1", status: "completed", progress: 100, url: "https://s3/v.mp4" });
    });
    const c = client(fetchMock);
    const video = await c.v1.videos.create({
      model: "veo-3-fast",
      prompt: "cat",
      seconds: 8,
      size: "1920x1080",
      input_reference: "https://example.com/frame.jpg",
      resolution: "1080p",
    });
    assert.equal(video.status, "queued");
    assert.equal(calls[0].url, "https://api.test/v1/videos");
    assert.deepEqual(calls[0].body, {
      model: "veo-3-fast",
      prompt: "cat",
      seconds: 8,
      size: "1920x1080",
      input_reference: "https://example.com/frame.jpg",
      resolution: "1080p",
    });
    assert.match(calls[0].headers["Idempotency-Key"], /^sdk-/);

    const done = await c.v1.videos.wait(video, 10_000, 1);
    assert.equal(done.url, "https://s3/v.mp4");
    assert.equal(polls, 3);
    assert.equal(calls.at(-1).url, "https://api.test/v1/videos/v-1");
  });

  it("v1.videos.retrieve escapes the id", async () => {
    const { fetchMock, calls } = mockFetch(() => json(200, { id: "x", status: "queued" }));
    await client(fetchMock).v1.videos.retrieve("../me/keys");
    assert.equal(calls[0].url, "https://api.test/v1/videos/..%2Fme%2Fkeys");
  });

  it("v1.videos.wait: failed → TaskFailedError, timeout → TaskTimeoutError", async () => {
    const failed = mockFetch(() =>
      json(200, { id: "v-1", status: "failed", error: { message: "provider down", type: "api_error" } }),
    );
    await assert.rejects(
      client(failed.fetchMock).v1.videos.wait("v-1", 1_000, 1),
      (e) => e instanceof TaskFailedError && e.message.includes("provider down") && e.responseBody.task_id === "v-1",
    );
    const pending = mockFetch(() => json(200, { id: "v-1", status: "queued" }));
    await assert.rejects(client(pending.fetchMock).v1.videos.wait("v-1", 5, 1), TaskTimeoutError);
  });
});

// ============================================================
// Обрыв долгого запроса после отправки, ключ в ошибках, отмена стрима
// ============================================================

import { getEventListeners } from "node:events";

describe("after-send failures and cancellation", () => {
  it("long requests are not retried after send; error carries the key", async () => {
    for (const call of [
      (c) => c.chat.completions.create({ model: "m", messages: [] }),
      (c) => c.v1.images.generate({ model: "m", prompt: "p" }),
    ]) {
      const { fetchMock, calls } = mockFetch(() => {
        throw new TypeError("fetch failed", { cause: { code: "UND_ERR_HEADERS_TIMEOUT" } });
      });
      await assert.rejects(call(client(fetchMock, { maxRetries: 3 })), (e) => {
        assert.ok(e instanceof NetworkError);
        assert.equal(e.idempotencyKey, calls[0].headers["Idempotency-Key"]);
        assert.ok(e.message.includes(e.idempotencyKey));
        return true;
      });
      assert.equal(calls.length, 1);
    }
  });

  it("long requests are retried when the request never left", { timeout: 10_000 }, async () => {
    const { fetchMock, calls } = mockFetch((_, n) => {
      if (n === 1) throw new TypeError("fetch failed", { cause: { code: "ECONNREFUSED" } });
      return json(200, COMPLETION);
    });
    const reply = await client(fetchMock, { maxRetries: 1 }).chat.completions.create({ model: "m", messages: [] });
    assert.deepEqual(reply, COMPLETION);
    assert.equal(calls.length, 2);
  });

  it("HTTP errors carry the idempotency key", async () => {
    const { fetchMock } = mockFetch(() => json(409, { detail: "still being processed" }));
    await assert.rejects(
      client(fetchMock).chat.completions.create({ model: "m", messages: [], idempotencyKey: "k-12345678" }),
      (e) => e instanceof ConflictError && e.idempotencyKey === "k-12345678",
    );
  });

  it("non-JSON error text becomes the message", async () => {
    const { fetchMock } = mockFetch(() => new Response("Bad Gateway from proxy", { status: 502 }));
    await assert.rejects(
      client(fetchMock).tasks.get("t-1"),
      (e) => e instanceof NexusError && e.status === 502 && e.message === "Bad Gateway from proxy",
    );
  });

  it("user signal during a stream → AbortError, listener removed after the stream", async () => {
    const ac = new AbortController();
    const { fetchMock } = mockFetch((call) => {
      const signal = call.signal;
      const body = new ReadableStream({
        start(controller) {
          controller.enqueue(new TextEncoder().encode(sse([chunk("a")], { done: false })));
          signal.addEventListener("abort", () => controller.error(signal.reason));
        },
      });
      return new Response(body, { status: 200 });
    });
    const c = client(fetchMock);
    const stream = await c.chat.completions.create({ model: "m", messages: [], stream: true, signal: ac.signal });
    await assert.rejects(
      (async () => {
        for await (const _ of stream) ac.abort();
      })(),
      (e) => e.name === "AbortError",
    );

    const longLived = new AbortController();
    const ok = mockFetch(() => new Response(sse([chunk("x")]), { status: 200 }));
    const c2 = client(ok.fetchMock);
    for (let i = 0; i < 3; i += 1) {
      const s = await c2.chat.completions.create({ model: "m", messages: [], stream: true, signal: longLived.signal });
      await collect(s);
    }
    assert.equal(getEventListeners(longLived.signal, "abort").length, 0);
  });
});
