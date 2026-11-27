/**
 * Внутренние константы и helpers, общие для нативных и /v1-ресурсов.
 * Не экспортируются из пакета.
 */

// node:crypto, а не globalThis.crypto: в Node 18 глобальный WebCrypto
// включается только флагом --experimental-global-webcrypto.
import { randomUUID } from "node:crypto";

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

export function generateIdempotencyKey(userKey?: string): string {
  if (userKey) return userKey;
  // Префикс sdk- помогает отличить в логах автогенерацию от user-provided.
  return `sdk-${randomUUID()}`;
}

export function backoffDelayMs(attempt: number): number {
  // 0 → 500-800, 1 → 1000-1300, 2 → 2000-2300, 3+ → 4000-4300.
  const base = Math.min(4000, 500 * 2 ** attempt);
  return base + Math.floor(Math.random() * 300);
}

export function sleep(ms: number): Promise<void> {
  return new Promise((r) => setTimeout(r, ms));
}

/** Копия объекта без undefined/null значений. */
export function compact(obj: Record<string, unknown>): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  for (const [k, v] of Object.entries(obj)) {
    if (v !== undefined && v !== null) out[k] = v;
  }
  return out;
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
    cur = (cur as { cause?: unknown }).cause;
  }
  return false;
}
