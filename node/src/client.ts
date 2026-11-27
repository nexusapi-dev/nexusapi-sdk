/**
 * NexusAPI клиент и Task-объект для Node 18+.
 *
 * Дизайн: OpenAI/Stripe-стиль, `client.<resource>.<verb>(...)`. Без зависимостей —
 * используется встроенный fetch.
 *
 * Ресурсы:
 * - нативный API: `videos`, `images`, `tasks`, `models` (`POST /generate`,
 *   `GET /tasks/{id}`, `GET /public/models`);
 * - OpenAI-совместимый `/v1`: `chat.completions` (в том числе SSE-стрим) и
 *   `v1.models`, `v1.images`, `v1.videos` (см. ./v1.ts).
 *
 * Reliability:
 * - **Retries on 5xx + 429 + network errors** — до `maxRetries` (дефолт 2)
 *   с экспоненциальным backoff (0.5s, 1s, 2s + jitter).
 * - **Auto Idempotency-Key** — каждому POST автоматически проставляется UUID v4,
 *   если клиент не передал свой. Гарантирует что retry не создаст дубль.
 *   Исключение — стрим чата: ключ уходит только если передан явно, а повтор
 *   делается только когда запрос не дошёл до сервера.
 */

import {
  AuthenticationError,
  BillingError,
  ConflictError,
  NetworkError,
  NexusError,
  NotFoundError,
  PermissionError,
  RateLimitError,
  TaskFailedError,
  TaskTimeoutError,
  ValidationError,
} from "./errors.js";
import {
  backoffDelayMs,
  DEFAULT_LONG_TIMEOUT_MS,
  generateIdempotencyKey,
  isNotSentError,
  RETRYABLE_STATUSES,
  sleep,
} from "./internal.js";
import { Chat, V1 } from "./v1.js";

const DEFAULT_BASE_URL = "https://nexusapi.dev";
const DEFAULT_TIMEOUT_MS = 60_000;
const DEFAULT_MAX_RETRIES = 2;
/** Версия SDK (уходит в User-Agent). Тест сверяет её с package.json. */
export const VERSION = "0.3.0";

// ============================================================
// Типы
// ============================================================

export type TaskStatus = "pending" | "queued" | "processing" | "completed" | "failed";

export interface TaskResult {
  video_url?: string;
  image_url?: string;
  image_urls?: string[];
  [k: string]: unknown;
}

export interface VideoCreateParams {
  model: string;
  prompt: string;
  duration?: number;
  aspect_ratio?: string;
  /** Опционально. При повторе с тем же ключом возвращает тот же task_id
   *  без второго списания. Если не передан — SDK сгенерит сам. См.
   *  docs.nexusapi.dev/concepts/idempotency. */
  idempotencyKey?: string;
  /** Любые native-параметры (webhook_url, negative_prompt, image_url, video_urls, ...) */
  [extra: string]: unknown;
}

export interface ImageGenerateParams {
  model: string;
  prompt: string;
  aspect_ratio?: string;
  /** Если true (default) — SDK сам ждёт завершения; иначе возвращает pending Task */
  wait?: boolean;
  /** Таймаут polling'а в мс (только если wait=true). Дефолт 180000. */
  timeout?: number;
  /** Интервал polling'а в мс. Дефолт 1500. */
  pollInterval?: number;
  /** Опционально. Если не передан — SDK сгенерит сам. */
  idempotencyKey?: string;
  /** Любые native-параметры (image_url для edit и т.д.) */
  [extra: string]: unknown;
}

export type PriceUnit = "sec" | "shot" | "request" | "token";

export interface PricingOption {
  id: string;
  label: string;
  priceRub: number;
  unit: PriceUnit;
  resolution?: string;
  duration?: number;
  mode?: string;
  [k: string]: unknown;
}

/** Запись публичного каталога GET /public/models. */
export interface ModelInfo {
  id: string;
  name: string;
  provider: string;
  kind: "video" | "image" | "audio" | "llm";
  unit: PriceUnit;
  priceRub: number;
  pricingOptions?: PricingOption[];
  /** Для LLM: цены за 1 млн токенов, ₽. */
  inputPriceRubPerMTokens?: number;
  outputPriceRubPerMTokens?: number;
  cacheReadPriceRubPerMTokens?: number;
  cacheCreationPriceRubPerMTokens?: number;
  supportsVision?: boolean;
  defaultPricingOptionId?: string;
  [k: string]: unknown;
}

export interface ClientOptions {
  apiKey: string;
  baseUrl?: string;
  /**
   * Per-request HTTP timeout. Default 60000. Для чата и /v1/images/generations
   * — не меньше 600000 (их можно переопределить параметром `timeoutMs` метода).
   */
  timeoutMs?: number;
  /** Max retry attempts on 5xx/429/network errors. Default 2. 0 to disable. */
  maxRetries?: number;
  /** Override fetch (for tests/proxies). Default = globalThis.fetch. */
  fetch?: typeof fetch;
}

// ============================================================
// Helpers (shared)
// ============================================================

interface ParsedError {
  message: string;
  errorClass: new (message: string, status?: number, body?: unknown) => NexusError;
  body: unknown;
}

async function parseErrorResponse(resp: Response): Promise<ParsedError> {
  let body: unknown;
  let message = `HTTP ${resp.status}`;
  let text = "";
  try {
    text = await resp.text();
  } catch {
    /* ignore */
  }
  try {
    body = JSON.parse(text);
    const b = body as {
      error?: { message?: string };
      detail?: string | Array<{ loc?: string[]; msg?: string }>;
    };
    if (b?.error?.message) {
      // OpenAI-shape (/v1/*)
      message = b.error.message;
    } else if (typeof b?.detail === "string") {
      message = b.detail;
    } else if (Array.isArray(b?.detail) && b.detail.length > 0) {
      const first = b.detail[0];
      const loc = (first?.loc ?? []).slice(1).join(".") || "request";
      const msg = first?.msg ?? "invalid";
      message = `${loc}: ${msg}`;
    }
  } catch {
    body = undefined;
    if (text) message = text;
  }
  const code = resp.status;
  const map: Record<number, ParsedError["errorClass"]> = {
    400: ValidationError,
    401: AuthenticationError,
    402: BillingError,
    403: PermissionError,
    404: NotFoundError,
    409: ConflictError,
    422: ValidationError,
    429: RateLimitError,
  };
  return { message, errorClass: map[code] ?? NexusError, body };
}

function buildGenerateParams(
  model: string,
  prompt: string,
  named: Record<string, unknown>,
  extras: Record<string, unknown>,
): Record<string, unknown> {
  const params: Record<string, unknown> = { model_name: model, prompt };
  for (const [k, v] of Object.entries(named)) {
    if (v !== undefined && v !== null) params[k] = v;
  }
  for (const [k, v] of Object.entries(extras)) {
    if (v !== undefined && v !== null) params[k] = v;
  }
  return params;
}

// ============================================================
// Task
// ============================================================

interface TaskInit {
  id: string;
  status: TaskStatus;
  model?: string;
  result?: TaskResult;
  error?: string;
  createdAt?: string;
}

export class Task {
  id: string;
  status: TaskStatus;
  model?: string;
  result?: TaskResult;
  error?: string;
  createdAt?: string;

  private readonly _client: NexusClient;

  constructor(client: NexusClient, init: TaskInit) {
    this._client = client;
    this.id = init.id;
    this.status = init.status;
    this.model = init.model;
    this.result = init.result;
    this.error = init.error;
    this.createdAt = init.createdAt;
  }

  get isDone(): boolean {
    return this.status === "completed" || this.status === "failed";
  }

  get videoUrl(): string | undefined {
    return this.result?.video_url as string | undefined;
  }

  get imageUrl(): string | undefined {
    const urls = this.result?.image_urls;
    if (Array.isArray(urls) && urls.length > 0) return urls[0];
    return this.result?.image_url as string | undefined;
  }

  get imageUrls(): string[] {
    const urls = this.result?.image_urls;
    return Array.isArray(urls) ? urls : [];
  }

  async refresh(): Promise<this> {
    const fresh = await this._client.tasks.get(this.id);
    this.status = fresh.status;
    this.result = fresh.result;
    this.error = fresh.error;
    this.createdAt = fresh.createdAt ?? this.createdAt;
    this.model = fresh.model ?? this.model;
    return this;
  }

  /**
   * Ожидает завершения задачи через polling.
   *
   * @param timeoutMs default 600000 (10 минут) для долгих видео-моделей
   * @param pollIntervalMs default 3000
   * @throws TaskTimeoutError если не дождались
   * @throws TaskFailedError если статус стал failed
   */
  async wait(
    timeoutMs: number = 600_000,
    pollIntervalMs: number = 3_000,
  ): Promise<this> {
    const deadline = Date.now() + timeoutMs;
    while (!this.isDone) {
      if (Date.now() > deadline) {
        throw new TaskTimeoutError(
          `Task ${this.id} did not complete within ${timeoutMs}ms`,
          { task_id: this.id, last_status: this.status },
        );
      }
      await sleep(pollIntervalMs);
      await this.refresh();
    }
    if (this.status === "failed") {
      throw new TaskFailedError(
        `Task ${this.id} failed: ${this.error ?? "unknown error"}`,
        { task_id: this.id, error: this.error },
      );
    }
    return this;
  }
}

// ============================================================
// Resources
// ============================================================

class Videos {
  constructor(private readonly client: NexusClient) {}

  async create(params: VideoCreateParams): Promise<Task> {
    const { model, prompt, duration, aspect_ratio, idempotencyKey, ...extras } = params;
    const apiParams = buildGenerateParams(model, prompt, { duration, aspect_ratio }, extras);
    const data = await this.client._request<{ task_id: string }>("POST", "/generate", {
      body: { params: apiParams },
      extraHeaders: { "Idempotency-Key": generateIdempotencyKey(idempotencyKey) },
    });
    return new Task(this.client, {
      id: data.task_id,
      status: "pending",
      model,
    });
  }
}

class Images {
  constructor(private readonly client: NexusClient) {}

  async generate(params: ImageGenerateParams): Promise<Task> {
    const {
      model,
      prompt,
      aspect_ratio,
      wait = true,
      timeout = 180_000,
      pollInterval = 1_500,
      idempotencyKey,
      ...extras
    } = params;
    const apiParams = buildGenerateParams(model, prompt, { aspect_ratio }, extras);
    const data = await this.client._request<{ task_id: string }>("POST", "/generate", {
      body: { params: apiParams },
      extraHeaders: { "Idempotency-Key": generateIdempotencyKey(idempotencyKey) },
    });
    const task = new Task(this.client, { id: data.task_id, status: "pending", model });
    if (wait) {
      await task.wait(timeout, pollInterval);
    }
    return task;
  }
}

class Tasks {
  constructor(private readonly client: NexusClient) {}

  async get(taskId: string): Promise<Task> {
    const data = await this.client._request<{
      task_id: string;
      status: TaskStatus;
      result?: TaskResult;
      error?: string;
      created_at?: string;
    }>("GET", `/tasks/${taskId}`);
    return new Task(this.client, {
      id: data.task_id,
      status: data.status,
      result: data.result,
      error: data.error,
      createdAt: data.created_at,
    });
  }
}

class Models {
  constructor(private readonly client: NexusClient) {}

  async list(): Promise<ModelInfo[]> {
    return this.client._request<ModelInfo[]>("GET", "/public/models", { auth: false });
  }
}

// ============================================================
// NexusClient
// ============================================================

/** @internal Опции транспорта. */
export interface RequestOptions {
  body?: unknown;
  auth?: boolean;
  extraHeaders?: Record<string, string>;
  /** Таймаут до получения заголовков ответа (для обычных запросов — и тела). */
  timeoutMs?: number;
  retryStatuses?: ReadonlySet<number>;
  /**
   * Повторять ли сетевые ошибки, когда запрос мог уже дойти до сервера.
   * По умолчанию true, для стрима false; долгие /v1-запросы передают false.
   */
  retrySentErrors?: boolean;
  signal?: AbortSignal;
}

export class NexusClient {
  private readonly apiKey: string;
  private readonly baseUrl: string;
  private readonly timeoutMs: number;
  private readonly maxRetries: number;
  private readonly fetchImpl: typeof fetch;

  readonly videos: Videos;
  readonly images: Images;
  readonly tasks: Tasks;
  readonly models: Models;
  /** OpenAI-совместимый POST /v1/chat/completions. */
  readonly chat: Chat;
  /** OpenAI-совместимые /v1/models, /v1/images/generations, /v1/videos. */
  readonly v1: V1;

  constructor(opts: ClientOptions) {
    if (!opts.apiKey) {
      throw new AuthenticationError("apiKey is required");
    }
    if (opts.maxRetries !== undefined && opts.maxRetries < 0) {
      throw new Error("maxRetries must be >= 0");
    }
    this.apiKey = opts.apiKey;
    this.baseUrl = (opts.baseUrl ?? DEFAULT_BASE_URL).replace(/\/+$/, "");
    this.timeoutMs = opts.timeoutMs ?? DEFAULT_TIMEOUT_MS;
    this.maxRetries = opts.maxRetries ?? DEFAULT_MAX_RETRIES;
    this.fetchImpl = opts.fetch ?? globalThis.fetch.bind(globalThis);

    this.videos = new Videos(this);
    this.images = new Images(this);
    this.tasks = new Tasks(this);
    this.models = new Models(this);
    this.chat = new Chat(this);
    this.v1 = new V1(this, this.chat);
  }

  /** @internal Таймаут долгого /v1-запроса: явный или не меньше 600000 мс. */
  _longTimeoutMs(override?: number): number {
    return override ?? Math.max(this.timeoutMs, DEFAULT_LONG_TIMEOUT_MS);
  }

  /**
   * @internal
   * Отправляет запрос с повторами и возвращает успешный Response.
   * stream=true: таймаут действует только до заголовков ответа, тело читает
   * Stream; повтор при сетевой ошибке — только если запрос точно не ушёл.
   */
  async _send(
    method: string,
    path: string,
    opts: RequestOptions & { stream?: boolean } = {},
  ): Promise<{ response: Response; controller: AbortController; cleanup: () => void }> {
    const {
      body,
      auth = true,
      extraHeaders,
      timeoutMs = this.timeoutMs,
      retryStatuses = RETRYABLE_STATUSES,
      signal,
      stream = false,
    } = opts;
    const retrySentErrors = opts.retrySentErrors ?? !stream;
    const url = `${this.baseUrl}${path}`;
    const headers: Record<string, string> = {
      "User-Agent": `nexusapi-node/${VERSION}`,
    };
    if (body !== undefined) headers["Content-Type"] = "application/json";
    if (auth) headers.Authorization = `Bearer ${this.apiKey}`;
    if (extraHeaders) Object.assign(headers, extraHeaders);
    const idempotencyKey = headers["Idempotency-Key"];

    let attempt = 0;
    // Цикл retries — выходим либо через return (success), либо через throw.
    // eslint-disable-next-line no-constant-condition
    while (true) {
      const controller = new AbortController();
      const onUserAbort = () => controller.abort(signal?.reason);
      const cleanup = () => signal?.removeEventListener("abort", onUserAbort);
      if (signal?.aborted) controller.abort(signal.reason);
      else signal?.addEventListener("abort", onUserAbort, { once: true });
      const timeoutHandle = setTimeout(() => controller.abort(), timeoutMs);

      let resp: Response;
      try {
        resp = await this.fetchImpl(url, {
          method,
          headers,
          body: body !== undefined ? JSON.stringify(body) : undefined,
          signal: controller.signal,
        });
      } catch (e) {
        clearTimeout(timeoutHandle);
        cleanup();
        // Отмена пользователем — не повторяем, пробрасываем как есть (AbortError).
        if (signal?.aborted) throw e;
        // fetch throws TypeError for network errors, AbortError for timeout.
        const notSent = isNotSentError(e);
        if ((retrySentErrors || notSent) && attempt < this.maxRetries) {
          await sleep(backoffDelayMs(attempt));
          attempt += 1;
          continue;
        }
        const reason = e instanceof Error ? e.message : String(e);
        let message = `Network error after ${attempt + 1} attempts: ${reason}`;
        if (idempotencyKey && !notSent) {
          message +=
            `. The request may still be running on the server: repeat the call with ` +
            `idempotencyKey "${idempotencyKey}" to get its result without a second charge`;
        }
        const err = new NetworkError(message);
        err.idempotencyKey = idempotencyKey;
        throw err;
      }
      clearTimeout(timeoutHandle);
      if (!stream) cleanup();

      if (retryStatuses.has(resp.status) && attempt < this.maxRetries) {
        // Honor Retry-After (seconds) if present.
        const retryAfter = resp.headers.get("retry-after");
        let delay = backoffDelayMs(attempt);
        if (retryAfter) {
          const parsed = Number(retryAfter);
          if (Number.isFinite(parsed) && parsed >= 0) {
            delay = parsed * 1000;
          }
        }
        await resp.body?.cancel().catch(() => undefined);
        cleanup();
        await sleep(delay);
        attempt += 1;
        continue;
      }

      if (!resp.ok) {
        cleanup();
        const { message, errorClass, body: errBody } = await parseErrorResponse(resp);
        const err = new errorClass(message, resp.status, errBody);
        err.idempotencyKey = idempotencyKey;
        throw err;
      }
      return { response: resp, controller, cleanup };
    }
  }

  /**
   * @internal
   * Generic typed request helper. Use the public videos/images/tasks/models
   * resources from user code.
   */
  async _request<T = unknown>(
    method: string,
    path: string,
    opts: RequestOptions = {},
  ): Promise<T> {
    const { response } = await this._send(method, path, opts);
    if (response.status === 204) return undefined as T;
    const text = await response.text();
    if (!text) return undefined as T;
    return JSON.parse(text) as T;
  }
}
