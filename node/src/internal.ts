/**
 * Внутренние константы и helpers, общие для нативных и /v1-ресурсов.
 * Не экспортируются из пакета.
 */

import { NetworkError, NexusError } from "./errors.js";

/** Ошибка опроса, которую стоит переждать, а не прерывать ожидание задачи. */
export function isTransientError(e: unknown): boolean {
  if (e instanceof NetworkError) return true;
  return e instanceof NexusError && e.status !== undefined && (e.status === 429 || e.status >= 500);
}

// Статусы которые retry'им. 429 — даём rate-limit отпустить, 5xx — серверная
// нестабильность которая может пройти сама.
export const RETRYABLE_STATUSES: ReadonlySet<number> = new Set([429, 500, 502, 503, 504]);

// Для долгих синхронных /v1-запросов (чат, /v1/images/generations) 500/502/504
// НЕ повторяем: сервер мог уже создать платную задачу (504 у /v1/images значит
// «задача ещё идёт»), а 5xx не кэшируется по Idempotency-Key — повтор создал бы
// вторую задачу и второе списание. 429 и 503 приходят до создания задачи.
export const SAFE_RETRY_STATUSES: ReadonlySet<number> = new Set([429, 503]);

// Чат и /v1/images/generations сервер держит долго: картинки до 180 с
// (Seedream Pro до 540 с), ответ LLM — минуты. Ждём не меньше 600 с.
// Учтите: встроенный fetch Node (undici) сам ждёт заголовки ответа не дольше
// 300 с; дольше — только со своим fetch (undici Agent с headersTimeout: 0).
export const DEFAULT_LONG_TIMEOUT_MS = 600_000;

// POST /generate у части моделей до ответа проверяет входные файлы (до 90 с).
export const DEFAULT_GENERATE_TIMEOUT_MS = 120_000;

// Больше Retry-After не ждём: вместо часового сна — backoff.
export const MAX_RETRY_AFTER_MS = 60_000;

// Сколько ждать, пока сервер закончит первый запрос, если повтор самого SDK
// получил 409 «ещё выполняется».
export const CONFLICT_WAIT_BUDGET_MS = 120_000;

// Максимум для setTimeout: больше Node превращает в 1 мс.
export const MAX_TIMER_MS = 2_147_483_647;

export function generateIdempotencyKey(userKey?: string): string {
  if (userKey) {
    validateIdempotencyKey(userKey);
    return userKey;
  }
  // Префикс sdk- помогает отличить в логах автогенерацию от user-provided.
  return `sdk-${randomUUID()}`;
}

/** Сервер принимает ключи 8–256 символов; проверяем до отправки. */
export function validateIdempotencyKey(key: string): void {
  if (key.length < 8 || key.length > 256) {
    throw new RangeError("idempotencyKey must be 8-256 characters long");
  }
  // Значение HTTP-заголовка: только печатные ASCII, иначе fetch бросит TypeError.
  if (!/^[\x20-\x7e]+$/.test(key)) {
    throw new RangeError("idempotencyKey must contain printable ASCII characters only");
  }
}

/**
 * UUID v4 без статического импорта node:crypto, чтобы пакет собирался и под
 * браузер/edge. В Node 18 без флага глобального crypto нет — тогда ключ из
 * Math.random: он не секретный, важна только уникальность.
 */
function randomUUID(): string {
  const c = (globalThis as { crypto?: { randomUUID?: () => string } }).crypto;
  if (typeof c?.randomUUID === "function") return c.randomUUID();
  const hex = Array.from({ length: 32 }, () => Math.floor(Math.random() * 16).toString(16));
  hex[12] = "4";
  hex[16] = ((parseInt(hex[16]!, 16) & 0x3) | 0x8).toString(16);
  const s = hex.join("");
  return `${s.slice(0, 8)}-${s.slice(8, 12)}-${s.slice(12, 16)}-${s.slice(16, 20)}-${s.slice(20)}`;
}

export function backoffDelayMs(attempt: number): number {
  // 0 → 500-625, 1 → 1000-1250, 2 → 2000-2500, 3+ → 4000-5000 (jitter до +25%).
  const base = Math.min(4000, 500 * 2 ** attempt);
  return Math.round(base * (1 + Math.random() * 0.25));
}

/**
 * Retry-After в мс: `retry-after-ms`, `retry-after` (секунды или HTTP-дата).
 * undefined, если заголовка нет или значение негодное (отрицательное, не число,
 * больше MAX_RETRY_AFTER_MS).
 */
export function parseRetryAfterMs(headers: Headers): number | undefined {
  let ms: number | undefined;
  const rawMs = headers.get("retry-after-ms");
  const raw = headers.get("retry-after");
  if (rawMs) {
    ms = Number(rawMs);
  } else if (raw) {
    const seconds = Number(raw);
    if (raw.trim() !== "" && !Number.isNaN(seconds)) {
      ms = seconds * 1000;
    } else {
      const date = Date.parse(raw);
      if (!Number.isNaN(date)) ms = date - Date.now();
    }
  }
  if (ms === undefined || !Number.isFinite(ms) || ms < 0 || ms > MAX_RETRY_AFTER_MS) {
    return undefined;
  }
  return ms;
}

/** Пауза, которую можно прервать signal'ом (тогда — reject с signal.reason). */
export function sleep(ms: number, signal?: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    if (signal?.aborted) {
      reject(abortReason(signal));
      return;
    }
    const onAbort = () => {
      clearTimeout(timer);
      reject(abortReason(signal!));
    };
    const timer = setTimeout(() => {
      signal?.removeEventListener("abort", onAbort);
      resolve();
    }, Math.min(Math.max(0, ms), MAX_TIMER_MS));
    signal?.addEventListener("abort", onAbort, { once: true });
  });
}

export function abortReason(signal: AbortSignal): unknown {
  return signal.reason ?? new DOMException("This operation was aborted", "AbortError");
}

/** Копия объекта без undefined/null значений. */
export function compact(obj: Record<string, unknown>): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  for (const [k, v] of Object.entries(obj)) {
    if (v !== undefined && v !== null) out[k] = v;
  }
  return out;
}

export function truncate(text: string, limit = 500): string {
  const t = text.trim();
  return t.length <= limit ? t : `${t.slice(0, limit)}…`;
}

// Коды ошибок, при которых запрос точно не ушёл на сервер (не подключились).
// Только их повторяем для SSE-стрима: стрим нельзя переотправить с середины.
const NOT_SENT_CODES = new Set([
  "ECONNREFUSED",
  "ENOTFOUND",
  "EAI_AGAIN",
  "ENETUNREACH",
  "EHOSTUNREACH",
  "UND_ERR_CONNECT_TIMEOUT",
]);

export function isNotSentError(e: unknown): boolean {
  let cur: unknown = e;
  for (let depth = 0; cur && depth < 4; depth += 1) {
    const code = (cur as { code?: unknown }).code;
    if (typeof code === "string" && NOT_SENT_CODES.has(code)) return true;
    // undici при двух адресах (IPv4/IPv6) складывает ошибки в AggregateError.
    const errs = (cur as { errors?: unknown }).errors;
    if (Array.isArray(errs) && errs.length > 0 && errs.every((x) => isNotSentError(x))) return true;
    cur = (cur as { cause?: unknown }).cause;
  }
  return false;
}
