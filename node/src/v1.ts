/**
 * OpenAI-совместимые эндпоинты /v1/*: chat.completions (в том числе SSE-стрим),
 * models, images.generations, videos.
 */

import type { NexusClient, RequestOptions, WaitOptions } from "./client.js";
import { NexusError, TaskFailedError, TaskTimeoutError } from "./errors.js";
import {
  CHAT_RETRY_STATUSES,
  CHAT_RETRY_WINDOW_MS,
  clock,
  compact,
  deadlineFrom,
  getHeader,
  IMAGES_RETRY_STATUSES,
  isTransientError,
  requestTransport,
  resolveIdempotencyKey,
  SAFE_RETRY_STATUSES,
  sleep,
  validateIdempotencyKey,
} from "./internal.js";
import { Stream } from "./streaming.js";

// ============================================================
// Типы: chat
// ============================================================

export interface ChatContentPart {
  type: "text" | "image_url" | string;
  text?: string;
  image_url?: { url: string; detail?: string };
  [k: string]: unknown;
}

export interface ChatMessage {
  role: "system" | "developer" | "user" | "assistant" | "tool" | string;
  content?: string | ChatContentPart[] | null;
  name?: string;
  tool_calls?: unknown[];
  tool_call_id?: string;
  [k: string]: unknown;
}

export type ReasoningEffort = "low" | "medium" | "high" | "xhigh" | "max";

export interface ChatCompletionCreateParams {
  /** Например "claude-sonnet-5", "gpt-6-luna". Список — client.v1.models.list(). */
  model: string;
  messages: ChatMessage[];
  /** true — SSE-стрим, create() вернёт Stream<ChatCompletionChunk>. */
  stream?: boolean;
  max_tokens?: number;
  max_completion_tokens?: number;
  temperature?: number;
  top_p?: number;
  top_k?: number;
  reasoning_effort?: ReasoningEffort;
  effort?: ReasoningEffort;
  thinking?: Record<string, unknown>;
  tools?: unknown[];
  tool_choice?: unknown;
  response_format?: unknown;
  /**
   * Без стрима SDK генерирует ключ сам: повтор после сбоя вернёт тот же ответ
   * без второго списания. Для стрима ключ уходит, только если передан явно.
   */
  idempotencyKey?: string;
  /** Сколько ждать ответа, мс. По умолчанию не меньше 600000. */
  timeoutMs?: number;
  /** Отмена запроса (для стрима — ещё и stream.abort()). */
  signal?: AbortSignal;
  /** Любые другие поля OpenAI Chat Completions — передаются как есть. */
  [extra: string]: unknown;
}

export interface ChatCompletionUsage {
  prompt_tokens: number;
  completion_tokens: number;
  total_tokens?: number;
  [k: string]: unknown;
}

export interface ChatCompletion {
  id: string;
  object: "chat.completion";
  created?: number;
  model: string;
  choices: Array<{
    index: number;
    message: {
      role: string;
      content: string | null;
      tool_calls?: unknown[];
      [k: string]: unknown;
    };
    finish_reason: string | null;
    [k: string]: unknown;
  }>;
  usage?: ChatCompletionUsage;
  [k: string]: unknown;
}

export interface ChatCompletionChunk {
  id: string;
  object: "chat.completion.chunk";
  created?: number;
  model: string;
  choices: Array<{
    index: number;
    delta: {
      role?: string;
      content?: string | null;
      tool_calls?: unknown[];
      [k: string]: unknown;
    };
    finish_reason: string | null;
    [k: string]: unknown;
  }>;
  usage?: ChatCompletionUsage | null;
  [k: string]: unknown;
}

// ============================================================
// Типы: models / images / videos
// ============================================================

export interface OpenAIModel {
  id: string;
  object: "model";
  created: number;
  owned_by: string;
}

export interface V1ImageGenerateParams {
  model: string;
  prompt: string;
  /** "1024x1024", "1280x720"… — сервер переводит в aspect_ratio. */
  size?: string;
  /** Для моделей с выбором качества (например, Seedream Pro). */
  quality?: string;
  seed?: number;
  idempotencyKey?: string;
  /** Сколько ждать ответа, мс. По умолчанию не меньше 600000. */
  timeoutMs?: number;
  /** Отмена запроса. */
  signal?: AbortSignal;
  /** Native-параметры модели (image_url, negative_prompt, …). */
  [extra: string]: unknown;
}

export interface ImagesResponse {
  created: number;
  data: Array<{ url: string }>;
}

export interface V1VideoCreateParams {
  model: string;
  prompt: string;
  /** Длительность, с. */
  seconds?: number | string;
  /** "1920x1080", "1080x1920"… */
  size?: string;
  /** URL кадра-референса (у Seedance 2.0 — массив до 9 URL). */
  input_reference?: string | string[];
  seed?: number;
  resolution?: string;
  idempotencyKey?: string;
  /** Отмена запроса. */
  signal?: AbortSignal;
  /** image_urls, video_urls, audio_urls, webhook_url, negative_prompt, … */
  [extra: string]: unknown;
}

export type VideoStatus = "queued" | "in_progress" | "completed" | "failed";

export interface VideoObject {
  id: string;
  object: "video";
  model: string;
  status: VideoStatus;
  progress: number;
  created_at: number;
  completed_at?: number | null;
  size?: string | null;
  seconds?: string | null;
  quality?: string | null;
  error?: { message: string; type?: string; code?: string | null } | null;
  /** Ссылка на готовое видео (когда status = "completed"). */
  url?: string | null;
}

// ============================================================
// Resources
// ============================================================

export class ChatCompletions {
  constructor(private readonly client: NexusClient) {}

  /** POST /v1/chat/completions. С `stream: true` — Stream<ChatCompletionChunk>. */
  create(
    params: ChatCompletionCreateParams & { stream: true },
    options?: RequestOptions,
  ): Promise<Stream<ChatCompletionChunk>>;
  create(
    params: ChatCompletionCreateParams & { stream?: false | undefined },
    options?: RequestOptions,
  ): Promise<ChatCompletion>;
  create(
    params: ChatCompletionCreateParams,
    options?: RequestOptions,
  ): Promise<ChatCompletion | Stream<ChatCompletionChunk>>;
  async create(
    params: ChatCompletionCreateParams,
    options: RequestOptions = {},
  ): Promise<ChatCompletion | Stream<ChatCompletionChunk>> {
    const { stream, idempotencyKey, timeoutMs: paramsTimeout, signal: paramsSignal, ...rest } = params;
    const body = compact(rest);
    const explicitTimeout = options.timeoutMs ?? paramsTimeout;
    const requestTimeout = this.client._longTimeoutMs(explicitTimeout);
    const deadlineMs = deadlineFrom(explicitTimeout);
    const signal = options.signal ?? paramsSignal;
    if (stream) {
      body.stream = true;
      const extraHeaders: Record<string, string> = { ...options.headers, Accept: "text/event-stream" };
      const streamKey = idempotencyKey ?? getHeader(options.headers, "Idempotency-Key");
      if (streamKey) {
        validateIdempotencyKey(streamKey);
        extraHeaders["Idempotency-Key"] = streamKey;
      }
      const { response, controller, cleanup } = await this.client._send("POST", "/v1/chat/completions", {
        body,
        extraHeaders,
        timeoutMs: requestTimeout,
        deadlineMs,
        maxRetries: options.maxRetries,
        retryStatuses: SAFE_RETRY_STATUSES,
        signal,
        stream: true,
      });
      return new Stream<ChatCompletionChunk>(response, controller, cleanup);
    }
    return this.client._request<ChatCompletion>("POST", "/v1/chat/completions", {
      body,
      extraHeaders: { ...options.headers, "Idempotency-Key": resolveIdempotencyKey(idempotencyKey, options.headers) },
      timeoutMs: requestTimeout,
      deadlineMs,
      retryWindowMs: CHAT_RETRY_WINDOW_MS,
      maxRetries: options.maxRetries,
      retryStatuses: CHAT_RETRY_STATUSES,
      signal,
    });
  }
}

export class Chat {
  readonly completions: ChatCompletions;

  constructor(client: NexusClient) {
    this.completions = new ChatCompletions(client);
  }
}

export class V1Models {
  constructor(private readonly client: NexusClient) {}

  /** GET /v1/models — модели, доступные ключу, в формате OpenAI. Нужен ключ. */
  async list(options: RequestOptions = {}): Promise<OpenAIModel[]> {
    const data = await this.client._request<{ data?: OpenAIModel[] } | undefined>(
      "GET",
      "/v1/models",
      requestTransport(options),
    );
    return data?.data ?? [];
  }
}

export class V1Images {
  constructor(private readonly client: NexusClient) {}

  /**
   * POST /v1/images/generations — синхронно: сервер ждёт готовую картинку
   * (до 180 с, Seedream Pro до 540 с) и возвращает `{ created, data: [{ url }] }`.
   */
  async generate(params: V1ImageGenerateParams, options: RequestOptions = {}): Promise<ImagesResponse> {
    const { idempotencyKey, timeoutMs, signal, ...rest } = params;
    return this.client._request<ImagesResponse>("POST", "/v1/images/generations", {
      body: compact(rest),
      extraHeaders: { ...options.headers, "Idempotency-Key": resolveIdempotencyKey(idempotencyKey, options.headers) },
      timeoutMs: this.client._longTimeoutMs(options.timeoutMs ?? timeoutMs),
      deadlineMs: deadlineFrom(options.timeoutMs ?? timeoutMs),
      retryStatuses: IMAGES_RETRY_STATUSES,
      maxRetries: options.maxRetries,
      signal: options.signal ?? signal,
    });
  }
}

function videoId(video: string | Pick<VideoObject, "id">): string {
  const id = typeof video === "string" ? video : video?.id;
  if (!id) throw new TypeError("video id is required");
  return id;
}

export class V1Videos {
  constructor(private readonly client: NexusClient) {}

  /** POST /v1/videos — создаёт видео (Sora-style), сразу возвращает объект со status "queued". */
  async create(params: V1VideoCreateParams, options: RequestOptions = {}): Promise<VideoObject> {
    const { idempotencyKey, signal, timeoutMs, ...rest } = params as V1VideoCreateParams & { timeoutMs?: number };
    const explicitTimeout = options.timeoutMs ?? timeoutMs;
    return this.client._request<VideoObject>("POST", "/v1/videos", {
      body: compact(rest),
      extraHeaders: { ...options.headers, "Idempotency-Key": resolveIdempotencyKey(idempotencyKey, options.headers) },
      timeoutMs: explicitTimeout ?? this.client._generateTimeoutMs(),
      deadlineMs: deadlineFrom(explicitTimeout),
      maxRetries: options.maxRetries,
      signal: options.signal ?? signal,
    });
  }

  /** GET /v1/videos/{id}. У готового видео ссылка в поле `url`. */
  async retrieve(video: string | Pick<VideoObject, "id">, options: RequestOptions = {}): Promise<VideoObject> {
    return this.client._request<VideoObject>(
      "GET",
      `/v1/videos/${encodeURIComponent(videoId(video))}`,
      requestTransport(options),
    );
  }

  /**
   * Опрашивает видео до completed. Принимает id или объект из create().
   * Временные ошибки опроса (сеть, 429, 5xx) пережидает до дедлайна.
   *
   * `wait(video, timeoutMs?, pollIntervalMs?)` или `wait(video, { timeoutMs, pollIntervalMs, signal })`.
   *
   * @throws TaskTimeoutError если не дождались — видео продолжает генерироваться
   * @throws TaskFailedError если status = "failed" (деньги возвращаются на ключ)
   */
  async wait(
    video: string | Pick<VideoObject, "id">,
    timeoutOrOptions?: number | WaitOptions,
    pollIntervalMs?: number,
  ): Promise<VideoObject> {
    const opts: WaitOptions =
      typeof timeoutOrOptions === "object" ? timeoutOrOptions : { timeoutMs: timeoutOrOptions, pollIntervalMs };
    const timeoutMs = opts.timeoutMs ?? 600_000;
    const interval = opts.pollIntervalMs ?? 3_000;
    const { signal } = opts;
    const id = videoId(video);
    const deadline = clock.now() + timeoutMs;
    let current: Partial<VideoObject> = { id };
    while (true) {
      try {
        current = await this.retrieve(id, { signal });
      } catch (e) {
        if (signal?.aborted || !isTransientError(e) || clock.now() >= deadline) {
          if (e instanceof NexusError) e.taskId = id;
          throw e;
        }
      }
      if (current.status === "failed") {
        const message = current.error?.message;
        const err = new TaskFailedError(`Video ${id} failed: ${message ?? "unknown error"}`, {
          task_id: id,
          error: message,
          video: current,
        });
        err.taskId = id;
        err.task = current;
        throw err;
      }
      if (current.status === "completed") return current as VideoObject;
      const remaining = deadline - clock.now();
      if (remaining <= 0) {
        const err = new TaskTimeoutError(
          `Video ${id} did not complete within ${timeoutMs}ms (last status: ${current.status}); ` +
            `it is still running — wait for it again, do not create a new one`,
          { task_id: id, last_status: current.status },
        );
        err.taskId = id;
        err.task = current;
        throw err;
      }
      await sleep(Math.min(interval, remaining), signal);
    }
  }
}

/** OpenAI-совместимые эндпоинты /v1/*. `v1.chat` — тот же объект, что `client.chat`. */
export class V1 {
  readonly chat: Chat;
  readonly models: V1Models;
  readonly images: V1Images;
  readonly videos: V1Videos;

  constructor(client: NexusClient, chat: Chat) {
    this.chat = chat;
    this.models = new V1Models(client);
    this.images = new V1Images(client);
    this.videos = new V1Videos(client);
  }
}
