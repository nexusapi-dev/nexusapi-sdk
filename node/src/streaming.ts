/**
 * SSE (text/event-stream) для chat.completions.create({ stream: true }).
 */

import { NetworkError, NexusError, StreamError } from "./errors.js";
import type { ChatCompletion } from "./v1.js";

type Json = Record<string, unknown>;

/** Ключи, которые нельзя присваивать объекту из чужих данных (prototype pollution). */
const UNSAFE_KEYS = new Set(["__proto__", "constructor", "prototype"]);

function isObject(value: unknown): value is Json {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function choiceIndex(value: unknown): number {
  return typeof value === "number" && Number.isInteger(value) && value >= 0 ? value : 0;
}

/**
 * Склеивает дельту так же, как сервер: строки дописываются (кроме `role`, `id`,
 * `type` — они заменяются), объекты сливаются, массивы дополняются, остальное
 * заменяется. `null` не затирает уже пришедшее значение.
 */
function mergeFragment(target: Json, fragment: Json): void {
  for (const [key, value] of Object.entries(fragment)) {
    if (UNSAFE_KEYS.has(key)) continue;
    if (value === null || value === undefined) {
      if (!Object.hasOwn(target, key)) target[key] = null;
      continue;
    }
    const old = target[key];
    if (typeof value === "string") {
      target[key] = key === "role" || key === "id" || key === "type" ? value : (typeof old === "string" ? old : "") + value;
    } else if (Array.isArray(value)) {
      const list = Array.isArray(old) ? old : (target[key] = []);
      (list as unknown[]).push(...(structuredClone(value) as unknown[]));
    } else if (isObject(value)) {
      const inner = isObject(old) ? old : (target[key] = {});
      mergeFragment(inner as Json, value);
    } else {
      target[key] = value;
    }
  }
}

/**
 * Собирает чанки `chat.completion.chunk` в один ответ `chat.completion` по тем же
 * правилам, что и сервер для обычного ответа: текст, reasoning, refusal, logprobs и
 * вызовы инструментов (их куски склеиваются по `index`), finish_reason и usage.
 * Неизвестные поля не теряются.
 */
class ChunkAccumulator {
  private readonly result: Json = { object: "chat.completion" };
  private readonly choices = new Map<number, Json>();
  private readonly tools = new Map<number, Map<number, Json>>();

  add(chunk: unknown): void {
    if (!isObject(chunk)) return;
    for (const [key, value] of Object.entries(chunk)) {
      if (key !== "object" && key !== "choices" && key !== "usage" && !UNSAFE_KEYS.has(key)) {
        this.result[key] = structuredClone(value);
      }
    }
    if (chunk.usage !== undefined && chunk.usage !== null) this.result.usage = structuredClone(chunk.usage);
    if (!Array.isArray(chunk.choices)) return;
    for (const choice of chunk.choices as unknown[]) {
      if (!isObject(choice)) continue;
      const index = choiceIndex(choice.index);
      let target = this.choices.get(index);
      if (!target) {
        target = { index, message: { role: "assistant", content: null }, finish_reason: null };
        this.choices.set(index, target);
      }
      const { tool_calls: fragments, ...delta } = isObject(choice.delta) ? choice.delta : {};
      mergeFragment(target.message as Json, delta);
      if (Array.isArray(fragments)) {
        let tools = this.tools.get(index);
        if (!tools) {
          tools = new Map();
          this.tools.set(index, tools);
        }
        for (const tool of fragments as unknown[]) {
          if (!isObject(tool)) continue;
          const { index: toolIndex, ...fragment } = tool;
          const key = choiceIndex(toolIndex);
          let slot = tools.get(key);
          if (!slot) {
            slot = {};
            tools.set(key, slot);
          }
          mergeFragment(slot, fragment);
        }
      }
      if (choice.finish_reason !== undefined && choice.finish_reason !== null) target.finish_reason = choice.finish_reason;
      if (isObject(choice.logprobs)) {
        if (!isObject(target.logprobs)) target.logprobs = {};
        mergeFragment(target.logprobs as Json, choice.logprobs);
      } else if ("logprobs" in choice && !Object.hasOwn(target, "logprobs")) {
        target.logprobs = null;
      }
      for (const [key, value] of Object.entries(choice)) {
        if (!["index", "delta", "finish_reason", "logprobs"].includes(key) && !UNSAFE_KEYS.has(key)) {
          target[key] = structuredClone(value);
        }
      }
    }
  }

  text(): string {
    const content = (this.choices.get(0)?.message as Json | undefined)?.content;
    return typeof content === "string" ? content : "";
  }

  completion(): ChatCompletion {
    const result = structuredClone(this.result);
    result.choices = [...this.choices.keys()]
      .sort((a, b) => a - b)
      .map((index) => {
        const choice = structuredClone(this.choices.get(index) as Json);
        const tools = this.tools.get(index);
        if (tools) {
          (choice.message as Json).tool_calls = [...tools.keys()]
            .sort((a, b) => a - b)
            .map((key) => structuredClone(tools.get(key)));
        }
        return choice;
      });
    return result as unknown as ChatCompletion;
  }
}

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
 *
 * `text()` и `finalCompletion()` учитывают и чанки, уже прочитанные в цикле, и
 * дочитывают остаток; повторный вызов вернёт тот же результат. После `break` или
 * `abort()` соединение закрыто — они вернут то, что успело прийти
 * (`finish_reason` — null).
 */
export class Stream<T> implements AsyncIterable<T> {
  readonly response: Response;
  private readonly controller: AbortController;
  private readonly onClose: () => void;
  private aborted = false;
  private iterator?: AsyncGenerator<T>;
  private failure?: { error: unknown };
  private readonly accumulator = new ChunkAccumulator();

  constructor(response: Response, controller: AbortController, onClose: () => void = () => undefined) {
    this.response = response;
    this.controller = controller;
    this.onClose = onClose;
  }

  /** Дочитывает стрим и возвращает весь текст ответа (первый вариант). */
  async text(): Promise<string> {
    return (await this.drain()).text();
  }

  /**
   * Дочитывает стрим и собирает обычный ответ `chat.completion`: текст, вызовы
   * инструментов, finish_reason и usage (если сервер его прислал).
   */
  async finalCompletion(): Promise<ChatCompletion> {
    return (await this.drain()).completion();
  }

  /** Прерывает стрим и закрывает соединение. Итерация завершится без ошибки. */
  abort(): void {
    this.aborted = true;
    this.controller.abort();
  }

  [Symbol.asyncIterator](): AsyncIterator<T> {
    if (this.iterator) {
      return (async function* (): AsyncGenerator<T> {
        throw new NexusError("Stream can only be iterated once");
      })();
    }
    this.iterator = this.iterate();
    return this.iterator;
  }

  private async drain(): Promise<ChunkAccumulator> {
    this.iterator ??= this.iterate();
    while (!(await this.iterator.next()).done) {
      // чанки уже в аккумуляторе
    }
    if (this.failure) throw this.failure.error;
    return this.accumulator;
  }

  private async *iterate(): AsyncGenerator<T> {
    try {
      yield* this.events();
    } catch (e) {
      this.failure = { error: e };
      throw e;
    }
  }

  private async *events(): AsyncGenerator<T> {
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
          this.accumulator.add(event);
          yield event as T;
        }
      }
      for (const payload of [...sse.feed(textDecoder.decode()), ...sse.flush()]) {
        const event = decodeStreamEvent(payload);
        if (event === DONE) return;
        this.accumulator.add(event);
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
