/**
 * SSE (text/event-stream) для chat.completions.create({ stream: true }).
 */

import { NetworkError, StreamError } from "./errors.js";

const LINE_BREAK = /\r\n|\r|\n/;

/** Разбирает поток текста в payload'ы `data:` событий SSE. */
export class SSEDecoder {
  private pending = "";
  private data: string[] = [];

  feed(chunk: string): string[] {
    let text = this.pending + chunk;
    // '\r' в конце может оказаться первой половиной '\r\n' — ждём следующий кусок.
    const holdCr = text.endsWith("\r");
    if (holdCr) text = text.slice(0, -1);
    const lines = text.split(LINE_BREAK);
    this.pending = (lines.pop() ?? "") + (holdCr ? "\r" : "");
    const events: string[] = [];
    for (const line of lines) {
      const event = this.line(line);
      if (event !== undefined) events.push(event);
    }
    return events;
  }

  flush(): string[] {
    const events: string[] = [];
    if (this.pending) {
      const event = this.line(this.pending.replace(/\r$/, ""));
      this.pending = "";
      if (event !== undefined) events.push(event);
    }
    if (this.data.length > 0) {
      events.push(this.data.join("\n"));
      this.data = [];
    }
    return events;
  }

  private line(line: string): string | undefined {
    if (line === "") {
      if (this.data.length === 0) return undefined;
      const event = this.data.join("\n");
      this.data = [];
      return event;
    }
    if (line.startsWith(":")) return undefined; // комментарий / keep-alive
    const colon = line.indexOf(":");
    const name = colon === -1 ? line : line.slice(0, colon);
    let value = colon === -1 ? "" : line.slice(colon + 1);
    if (value.startsWith(" ")) value = value.slice(1);
    if (name === "data") this.data.push(value);
    return undefined;
  }
}

const DONE = Symbol("done");

function decodeStreamEvent(payload: string): unknown {
  if (payload.trim() === "[DONE]") return DONE;
  let event: unknown;
  try {
    event = JSON.parse(payload);
  } catch {
    throw new StreamError(`Malformed stream event: ${payload.slice(0, 200)}`, payload);
  }
  const err = (event as { error?: { message?: string } } | null)?.error;
  if (err && typeof err === "object") {
    throw new StreamError(err.message || "Stream error", event);
  }
  return event;
}

/**
 * SSE-стрим чанков. Итерируется один раз через `for await`.
 *
 * ```ts
 * const stream = await client.chat.completions.create({ ..., stream: true });
 * for await (const chunk of stream) {
 *   process.stdout.write(chunk.choices[0]?.delta?.content ?? "");
 * }
 * ```
 *
 * Соединение закрывается, когда стрим дочитан, при `break` из цикла или при
 * `abort()`. Обрыв посреди стрима — NetworkError (без повтора); ошибка,
 * присланная сервером внутри стрима, — StreamError.
 */
export class Stream<T> implements AsyncIterable<T> {
  readonly response: Response;
  private readonly controller: AbortController;
  private readonly onClose: () => void;
  private consumed = false;
  private aborted = false;

  constructor(response: Response, controller: AbortController, onClose: () => void = () => undefined) {
    this.response = response;
    this.controller = controller;
    this.onClose = onClose;
  }

  /** Прерывает стрим и закрывает соединение. Итерация завершится без ошибки. */
  abort(): void {
    this.aborted = true;
    this.controller.abort();
  }

  async *[Symbol.asyncIterator](): AsyncIterator<T> {
    if (this.consumed) {
      throw new Error("Stream can only be iterated once");
    }
    this.consumed = true;
    const body = this.response.body;
    if (!body) return;
    const reader = body.getReader();
    const textDecoder = new TextDecoder();
    const sse = new SSEDecoder();
    try {
      while (true) {
        let result: Awaited<ReturnType<typeof reader.read>>;
        try {
          result = await reader.read();
        } catch (e) {
          // После заголовков контроллер прерывают только abort() или signal
          // пользователя: abort() — тихо завершаем, signal — отдаём AbortError.
          if (this.controller.signal.aborted) {
            if (this.aborted) return;
            throw e;
          }
          const reason = e instanceof Error ? e.message : String(e);
          throw new NetworkError(`Stream interrupted: ${reason}`);
        }
        if (result.done) break;
        for (const payload of sse.feed(textDecoder.decode(result.value, { stream: true }))) {
          const event = decodeStreamEvent(payload);
          if (event === DONE) return;
          yield event as T;
        }
      }
      for (const payload of [...sse.feed(textDecoder.decode()), ...sse.flush()]) {
        const event = decodeStreamEvent(payload);
        if (event === DONE) return;
        yield event as T;
      }
    } finally {
      // Закрываем соединение в любом случае: дочитали, вышли через break,
      // получили ошибку или [DONE] пришёл раньше конца тела.
      await reader.cancel().catch(() => undefined);
      this.onClose();
    }
  }
}
