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
 *   с экспоненциальным backoff (0.5s, 1s, 2s + jitter), Retry-After до 60 с.
 * - **Auto Idempotency-Key** — каждому POST автоматически проставляется UUID v4,
 *   если клиент не передал свой: повтор SDK не создаёт вторую задачу. Если
 *   повтор получил 409 «первый запрос ещё выполняется», SDK ждёт его ответ.
 *   Исключение — стрим чата: ключ уходит только если передан явно, а повтор
 *   делается только когда запрос не дошёл до сервера.
 */

import {
  APIResponseValidationError,
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
  CONFLICT_WAIT_BUDGET_MS,
  DEFAULT_GENERATE_TIMEOUT_MS,
  DEFAULT_LONG_TIMEOUT_MS,
  generateIdempotencyKey,
  isNotSentError,
  isTransientError,
  MAX_TIMER_MS,
  parseRetryAfterMs,
  RETRYABLE_STATUSES,
  sleep,
  truncate,
} from "./internal.js";
import { Chat, V1 } from "./v1.js";

const DEFAULT_BASE_URL = "https://nexusapi.dev";
const DEFAULT_TIMEOUT_MS = 60_000;
const DEFAULT_MAX_RETRIES = 2;
/** Версия SDK (уходит в User-Agent). Тест сверяет её с package.json. */
export const VERSION = "0.3.1";

// ============================================================
// Типы
// ============================================================

export type TaskStatus = "pending" | "queued" | "processing" | "completed" | "failed";

export interface TaskResult {
  video_url?: string;
  image_url?: string;
  image_urls?: string[];
  audio_url?: string;
  audio_urls?: string[];
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
  /** Таймаут polling'а в мс (только если wait=true). Дефолт 600000. */
  timeout?: number;
  /** Интервал polling'а в мс. Дефолт 1500. */
  pollInterval?: number;
  /** Опционально. Если не передан — SDK сгенерит сам. */
  idempotencyKey?: string;
  /** Любые native-параметры (image_url для edit и т.д.) */
  [extra: string]: unknown;
}

export interface WaitOptions {
  /** Сколько ждать, мс. По умолчанию 600000. */
  timeoutMs?: number;
  /** Интервал опроса, мс. По умолчанию 3000. */
  pollIntervalMs?: number;
  /** Отмена ожидания (задача на сервере продолжит идти). */
  signal?: AbortSignal;
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
   * Таймаут запроса, мс (вместе с чтением ответа). Default 60000. Для
   * POST /generate — не меньше 120000, для чата и /v1/images/generations —
   * не меньше 600000 (их можно переопределить параметром `timeoutMs` метода).
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

type ErrorClass = new (message: string, status?: number, body?: unknown) => NexusError;

const STATUS_ERRORS: Record<number, ErrorClass> = {
  400: ValidationError,
  401: AuthenticationError,
  402: BillingError,
  403: PermissionError,
  404: NotFoundError,
  409: ConflictError,
  422: ValidationError,
  429: RateLimitError,
};

/** Сообщение об ошибке из тела ответа любой формы, не длиннее 500 символов. */
function errorMessage(body: unknown, text: string, status: number): string {
  let message: unknown;
  if (body && typeof body === "object" && !Array.isArray(body)) {
    const { error, detail } = body as { error?: unknown; detail?: unknown };
    if (error && typeof error === "object") message = (error as { message?: unknown }).message;
    else if (typeof error === "string") message = error;
    if (!message) {
      if (typeof detail === "string") {
        message = detail;
      } else if (Array.isArray(detail) && detail.length > 0) {
        const first = detail[0] as unknown;
        if (first && typeof first === "object") {
          // Pydantic-валидация: [{ loc: [...], msg }]
          const { loc, msg } = first as { loc?: unknown; msg?: unknown };
          const path = Array.isArray(loc) ? loc.slice(1).join(".") : "";
          message = `${path || "request"}: ${typeof msg === "string" ? msg : "invalid"}`;
        } else {
          message = String(first);
        }
      } else if (detail && typeof detail === "object") {
        const d = detail as { message?: unknown; msg?: unknown };
        message = d.message ?? d.msg ?? JSON.stringify(detail);
      }
    }
  } else if (text) {
    message = text;
  }
  return message ? truncate(String(message)) : `HTTP ${status}`;
}

function headersToObject(headers: Headers): Record<string, string> {
  const out: Record<string, string> = {};
  headers.forEach((value, key) => {
    out[key] = value;
  });
  return out;
}

function statusError(resp: Response, text: string, idempotencyKey?: string): NexusError {
  let body: unknown;
  try {
    body = text ? JSON.parse(text) : undefined;
  } catch {
    body = text ? truncate(text, 2000) : undefined;
  }
  const ErrorCtor = STATUS_ERRORS[resp.status] ?? NexusError;
  const err = new ErrorCtor(errorMessage(body, text, resp.status), resp.status, body);
  err.headers = headersToObject(resp.headers);
  err.idempotencyKey = idempotencyKey;
  return err;
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

function requireTaskId(data: unknown, idempotencyKey?: string): string {
  const taskId = (data as { task_id?: unknown } | null | undefined)?.task_id;
  if (typeof taskId !== "string" || !taskId) {
    const err = new APIResponseValidationError("Server response has no task_id", undefined, data);
    err.idempotencyKey = idempotencyKey;
    throw err;
  }
  return taskId;
}

function normalizeBaseUrl(baseUrl: string): string {
  // Срезаем завершающие "/" циклом, а не /\/+$/: у такого регэкспа
  // квадратичное время на строках из множества "/" (CodeQL js/polynomial-redos).
  let end = baseUrl.length;
  while (end > 0 && baseUrl.charCodeAt(end - 1) === 47) end -= 1;
  let url = baseUrl.slice(0, end);
  let parsed: URL;
  try {
    parsed = new URL(url);
  } catch {
    throw new TypeError(`baseUrl must be an http(s) URL, got "${baseUrl}"`);
  }
  if (parsed.protocol !== "http:" && parsed.protocol !== "https:") {
    throw new TypeError(`baseUrl must be an http(s) URL, got "${baseUrl}"`);
  }
  if (url.endsWith("/v1")) {
    url = url.slice(0, -"/v1".length);
    const proc = (globalThis as { process?: { emitWarning?: (msg: string) => void } }).process;
    proc?.emitWarning?.(`nexusapi-sdk: baseUrl should not end with /v1 (the SDK adds it where needed); using ${url}`);
  }
  return url;
}

function validateTimeout(name: string, value: number): void {
  if (!Number.isFinite(value) || value <= 0 || value > MAX_TIMER_MS) {
    throw new RangeError(`${name} must be a positive number of milliseconds up to ${MAX_TIMER_MS}`);
  }
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

  // #private: не попадает в console.log / JSON и не тянет за собой клиент с ключом.
  readonly #client: NexusClient;

  constructor(client: NexusClient, init: TaskInit) {
    this.#client = client;
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

  #result(): TaskResult {
    const r = this.result as unknown;
    return r && typeof r === "object" && !Array.isArray(r) ? (r as TaskResult) : {};
  }

  get videoUrl(): string | undefined {
    const url = this.#result().video_url;
    return typeof url === "string" ? url : undefined;
  }

  get imageUrl(): string | undefined {
    const urls = this.imageUrls;
    if (urls.length > 0) return urls[0];
    const url = this.#result().image_url;
    return typeof url === "string" ? url : undefined;
  }

  get imageUrls(): string[] {
    const urls = this.#result().image_urls;
    return Array.isArray(urls) ? urls : [];
  }

  /** audio_urls[0] (Suno и др.) или audio_url. */
  get audioUrl(): string | undefined {
    const urls = this.audioUrls;
    if (urls.length > 0) return urls[0];
    const url = this.#result().audio_url;
    return typeof url === "string" ? url : undefined;
  }

  get audioUrls(): string[] {
    const urls = this.#result().audio_urls;
    return Array.isArray(urls) ? urls : [];
  }

  /** Данные задачи без ссылки на клиент — для JSON.stringify и логов. */
  toJSON(): TaskInit {
    return {
      id: this.id,
      status: this.status,
      model: this.model,
      result: this.result,
      error: this.error,
      createdAt: this.createdAt,
    };
  }

  async refresh(options: { signal?: AbortSignal } = {}): Promise<this> {
    const fresh = await this.#client.tasks.get(this.id, options);
    this.status = fresh.status;
    this.result = fresh.result;
    this.error = fresh.error;
    this.createdAt = fresh.createdAt ?? this.createdAt;
    this.model = fresh.model ?? this.model;
    return this;
  }

  /**
   * Ожидает завершения задачи через polling. Временные ошибки опроса (сеть,
   * 429, 5xx) пережидает до дедлайна.
   *
   * `wait(timeoutMs?, pollIntervalMs?)` или `wait({ timeoutMs, pollIntervalMs, signal })`.
   *
   * @throws TaskTimeoutError если не дождались — задача продолжает идти, `.task`
   *   можно дождаться ещё раз
   * @throws TaskFailedError если статус стал failed
   */
  async wait(timeoutOrOptions?: number | WaitOptions, pollIntervalMs?: number): Promise<this> {
    const opts: WaitOptions =
      typeof timeoutOrOptions === "object" ? timeoutOrOptions : { timeoutMs: timeoutOrOptions, pollIntervalMs };
    const timeoutMs = opts.timeoutMs ?? 600_000;
    const interval = opts.pollIntervalMs ?? 3_000;
    const { signal } = opts;
    const deadline = Date.now() + timeoutMs;
    while (!this.isDone) {
      const remaining = deadline - Date.now();
      if (remaining <= 0) {
        throw this.#attach(
          new TaskTimeoutError(
            `Task ${this.id} did not complete within ${timeoutMs}ms (last status: ${this.status}); ` +
              `it is still running — wait for it again, do not create a new one`,
            { task_id: this.id, last_status: this.status },
          ),
        );
      }
      await sleep(Math.min(interval, remaining), signal);
      try {
        await this.refresh({ signal });
      } catch (e) {
        if (signal?.aborted || !isTransientError(e) || Date.now() >= deadline) {
          if (e instanceof NexusError) this.#attach(e);
          throw e;
        }
      }
    }
    if (this.status === "failed") {
      throw this.#attach(
        new TaskFailedError(`Task ${this.id} failed: ${this.error ?? "unknown error"}`, {
          task_id: this.id,
          error: this.error,
        }),
      );
    }
    return this;
  }

  #attach<E extends NexusError>(err: E): E {
    err.taskId = this.id;
    err.task = this;
    return err;
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
    const key = generateIdempotencyKey(idempotencyKey);
    const data = await this.client._request<unknown>("POST", "/generate", {
      body: { params: apiParams },
      extraHeaders: { "Idempotency-Key": key },
      timeoutMs: this.client._generateTimeoutMs(),
    });
    return new Task(this.client, { id: requireTaskId(data, key), status: "pending", model });
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
      timeout = 600_000,
      pollInterval = 1_500,
      idempotencyKey,
      ...extras
    } = params;
    const apiParams = buildGenerateParams(model, prompt, { aspect_ratio }, extras);
    const key = generateIdempotencyKey(idempotencyKey);
    const data = await this.client._request<unknown>("POST", "/generate", {
      body: { params: apiParams },
      extraHeaders: { "Idempotency-Key": key },
      timeoutMs: this.client._generateTimeoutMs(),
    });
    const task = new Task(this.client, { id: requireTaskId(data, key), status: "pending", model });
    if (wait) {
      await task.wait(timeout, pollInterval);
    }
    return task;
  }
}

class Tasks {
  constructor(private readonly client: NexusClient) {}

  async get(taskId: string, options: { signal?: AbortSignal } = {}): Promise<Task> {
    if (!taskId) throw new TypeError("taskId is required");
    const data = await this.client._request<{
      task_id?: string;
      status?: TaskStatus;
      result?: TaskResult;
      error?: string;
      created_at?: string;
    }>("GET", `/tasks/${encodeURIComponent(taskId)}`, { signal: options.signal });
    const id = requireTaskId(data);
    if (typeof data.status !== "string") {
      throw new APIResponseValidationError("Server response has no task status", undefined, data);
    }
    return new Task(this.client, {
      id,
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
  /** Таймаут запроса, мс: для обычных запросов — вместе с чтением тела, для стрима — до заголовков. */
  timeoutMs?: number;
  retryStatuses?: ReadonlySet<number>;
  /**
   * Повторять ли сетевые ошибки, когда запрос мог уже дойти до сервера.
   * По умолчанию true, для стрима false; долгие /v1-запросы передают false.
   */
  retrySentErrors?: boolean;
  signal?: AbortSignal;
}

/** @internal */
export interface SendResult {
  response: Response;
  /** Тело ответа (для stream=false). */
  text: string;
  controller: AbortController;
  cleanup: () => void;
}

export class NexusClient {
  // #private: ключ не виден в console.log / util.inspect / JSON.
  readonly #apiKey: string;
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
    const maxRetries = opts.maxRetries ?? DEFAULT_MAX_RETRIES;
    if (!Number.isInteger(maxRetries) || maxRetries < 0) {
      throw new Error("maxRetries must be >= 0 (an integer)");
    }
    const timeoutMs = opts.timeoutMs ?? DEFAULT_TIMEOUT_MS;
    validateTimeout("timeoutMs", timeoutMs);
    this.#apiKey = opts.apiKey;
    this.baseUrl = normalizeBaseUrl(opts.baseUrl ?? DEFAULT_BASE_URL);
    this.timeoutMs = timeoutMs;
    this.maxRetries = maxRetries;
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
    const value = override ?? Math.max(this.timeoutMs, DEFAULT_LONG_TIMEOUT_MS);
    validateTimeout("timeoutMs", value);
    return value;
  }

  /** @internal Таймаут создания задачи (POST /generate, /v1/videos): не меньше 120000 мс. */
  _generateTimeoutMs(): number {
    return Math.max(this.timeoutMs, DEFAULT_GENERATE_TIMEOUT_MS);
  }

  /**
   * @internal
   * Отправляет запрос с повторами и возвращает успешный ответ.
   * stream=false: таймаут и signal покрывают и чтение тела (оно в `text`).
   * stream=true: таймаут действует только до заголовков, тело читает Stream;
   * повтор при сетевой ошибке — только если запрос точно не ушёл.
   */
  async _send(method: string, path: string, opts: RequestOptions & { stream?: boolean } = {}): Promise<SendResult> {
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
    if (auth) headers.Authorization = `Bearer ${this.#apiKey}`;
    if (extraHeaders) Object.assign(headers, extraHeaders);
    const idempotencyKey = headers["Idempotency-Key"];
    // Сериализуем один раз и до цикла: ошибка в данных — не повод для повторов.
    const payload = body !== undefined ? JSON.stringify(body) : undefined;

    let attempt = 0;
    let conflictAttempt = 0;
    let conflictDeadline: number | undefined;
    // Цикл retries — выходим либо через return (success), либо через throw.
    // eslint-disable-next-line no-constant-condition
    while (true) {
      const controller = new AbortController();
      const onUserAbort = () => controller.abort(signal?.reason);
      const cleanup = () => signal?.removeEventListener("abort", onUserAbort);
      if (signal?.aborted) controller.abort(signal.reason);
      else signal?.addEventListener("abort", onUserAbort, { once: true });
      let timedOut = false;
      const timeoutHandle = setTimeout(() => {
        timedOut = true;
        controller.abort();
      }, timeoutMs);

      let resp: Response;
      let text = "";
      try {
        resp = await this.fetchImpl(url, {
          method,
          headers,
          body: payload,
          signal: controller.signal,
        });
        // Тело читаем под тем же таймаутом и signal (кроме удачного стрима).
        if (!stream || !resp.ok) text = await resp.text();
      } catch (e) {
        clearTimeout(timeoutHandle);
        cleanup();
        // Отмена пользователем — не повторяем, пробрасываем как есть (AbortError).
        if (signal?.aborted) throw e;
        const notSent = isNotSentError(e);
        if ((retrySentErrors || notSent) && attempt < this.maxRetries) {
          await sleep(backoffDelayMs(attempt), signal);
          attempt += 1;
          continue;
        }
        throw networkError(e, { attempt, notSent, stream, timedOut, timeoutMs, idempotencyKey });
      }
      clearTimeout(timeoutHandle);
      if (!stream || !resp.ok) cleanup();

      if (retryStatuses.has(resp.status) && attempt < this.maxRetries) {
        await sleep(parseRetryAfterMs(resp.headers) ?? backoffDelayMs(attempt), signal);
        attempt += 1;
        continue;
      }

      // 409 после повтора самого SDK: первый запрос с этим ключом ещё выполняется
      // на сервере. Ждём его сохранённый ответ, а не сдаёмся.
      if (resp.status === 409 && idempotencyKey && attempt > 0 && !stream) {
        const now = Date.now();
        conflictDeadline ??= now + CONFLICT_WAIT_BUDGET_MS;
        if (now < conflictDeadline) {
          await sleep(Math.min(backoffDelayMs(conflictAttempt), 5_000, conflictDeadline - now), signal);
          conflictAttempt += 1;
          continue;
        }
      }

      if (!resp.ok) throw statusError(resp, text, idempotencyKey);
      return { response: resp, text, controller, cleanup };
    }
  }

  /**
   * @internal
   * Generic typed request helper. Use the public videos/images/tasks/models
   * resources from user code.
   */
  async _request<T = unknown>(method: string, path: string, opts: RequestOptions = {}): Promise<T> {
    const { response, text } = await this._send(method, path, opts);
    if (response.status === 204 || !text) return undefined as T;
    try {
      return JSON.parse(text) as T;
    } catch {
      const err = new APIResponseValidationError(
        `Invalid JSON in HTTP ${response.status} response`,
        response.status,
        truncate(text, 2000),
      );
      err.headers = headersToObject(response.headers);
      err.idempotencyKey = opts.extraHeaders?.["Idempotency-Key"];
      throw err;
    }
  }
}

function networkError(
  e: unknown,
  ctx: {
    attempt: number;
    notSent: boolean;
    stream: boolean;
    timedOut: boolean;
    timeoutMs: number;
    idempotencyKey?: string;
  },
): NetworkError {
  const reason = ctx.timedOut ? `timed out after ${ctx.timeoutMs}ms` : e instanceof Error ? e.message : String(e);
  let message: string;
  if (ctx.notSent) {
    message = `Could not connect after ${ctx.attempt + 1} attempts: ${reason}`;
  } else {
    message = `Network error after ${ctx.attempt + 1} attempts: ${reason}`;
    if (ctx.stream) {
      message += ". The request may have reached the server; a streamed response cannot be replayed";
    } else if (ctx.idempotencyKey) {
      message +=
        `. The request reached the server and may still be running (and be charged): repeat it with ` +
        `idempotencyKey "${ctx.idempotencyKey}", not a new key — once it has finished successfully ` +
        `the server returns the stored result`;
    }
  }
  const err = new NetworkError(message);
  err.idempotencyKey = ctx.idempotencyKey;
  (err as { cause?: unknown }).cause = e;
  return err;
}

