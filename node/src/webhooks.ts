/**
 * Проверка подписи вебхуков NexusAPI (WebCrypto: Node 20+, Deno, Bun, edge).
 *
 * Каждый вебхук приходит с заголовками:
 *
 * ```
 * X-Nexus-Timestamp: 1764288000
 * X-Nexus-Signature: v1=<hex HMAC-SHA256(secret, "{timestamp}.{сырое тело}")>
 * ```
 *
 * Секрет — `whsec_…`: его отдаёт `client.webhooks.secret()` (GET /webhooks/secret),
 * и он же вычисляется из API-ключа, которым создана задача. По секрету нельзя
 * восстановить ключ, поэтому сервису, который только принимает вебхуки,
 * достаточно секрета.
 *
 * ```ts
 * import { unwrapWebhook, WebhookVerificationError } from "nexusapi-sdk";
 *
 * app.post("/nexus-callback", express.raw({ type: "application/json" }), async (req, res) => {
 *   try {
 *     const event = await unwrapWebhook(req.body, req.headers, process.env.NEXUS_WEBHOOK_SECRET!);
 *     // event.task_id, event.status, event.result
 *     res.sendStatus(200);
 *   } catch (e) {
 *     if (e instanceof WebhookVerificationError) return res.sendStatus(400);
 *     throw e;
 *   }
 * });
 * ```
 */

import { NexusError, WebhookVerificationError } from "./errors.js";

export const SIGNATURE_HEADER = "X-Nexus-Signature";
export const TIMESTAMP_HEADER = "X-Nexus-Timestamp";
/** Допустимое расхождение часов отправителя и получателя, с. */
export const DEFAULT_WEBHOOK_TOLERANCE_SECONDS = 300;

const SIGNATURE_VERSION = "v1";
const SECRET_PREFIX = "whsec_";
const DERIVATION_LABEL = "nexus-webhook-signing-v1";

/** Тело вебхука о завершении задачи. */
export interface WebhookEvent {
  task_id: string;
  status: "completed" | "failed";
  result?: unknown;
  error?: string | null;
  [k: string]: unknown;
}

/** Сырое тело запроса: строка или байты — ровно то, что пришло. */
export type WebhookPayload = string | Uint8Array | ArrayBuffer;

/** Заголовки: Headers (fetch), объект Node/Express (`req.headers`) или Map. */
export type WebhookHeaders =
  | Headers
  | Map<string, string>
  | Record<string, string | string[] | undefined>;

export interface VerifyWebhookOptions {
  /** На сколько секунд timestamp может отличаться от текущего времени. 0 — не проверять. */
  toleranceSeconds?: number;
  /** Текущее время, с (для тестов). */
  nowSeconds?: number;
}

const encoder = new TextEncoder();

/** Нужная часть WebCrypto (без зависимости от DOM-типов в .d.ts). */
interface SubtleLike {
  importKey(
    format: "raw",
    keyData: Uint8Array,
    algorithm: { name: "HMAC"; hash: "SHA-256" },
    extractable: boolean,
    usages: ["sign"],
  ): Promise<unknown>;
  sign(algorithm: "HMAC", key: unknown, data: Uint8Array): Promise<ArrayBuffer>;
}

function subtle(): SubtleLike {
  const s = (globalThis as { crypto?: { subtle?: SubtleLike } }).crypto?.subtle;
  if (!s) throw new NexusError("WebCrypto (globalThis.crypto.subtle) is not available in this runtime");
  return s;
}

async function hmacSha256Hex(key: string, message: Uint8Array): Promise<string> {
  const cryptoKey = await subtle().importKey(
    "raw",
    encoder.encode(key),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"],
  );
  const sig = new Uint8Array(await subtle().sign("HMAC", cryptoKey, message));
  let hex = "";
  for (const byte of sig) hex += byte.toString(16).padStart(2, "0");
  return hex;
}

/** Сравнение строк за время, не зависящее от совпавшего префикса. */
function timingSafeEqual(a: string, b: string): boolean {
  if (a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i += 1) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}

function headerValue(headers: WebhookHeaders, name: string): string | undefined {
  let value: unknown;
  if (typeof (headers as Headers).get === "function" && !(headers instanceof Map)) {
    value = (headers as Headers).get(name) ?? undefined;
  } else {
    const lower = name.toLowerCase();
    const entries = headers instanceof Map ? headers.entries() : Object.entries(headers);
    for (const [key, item] of entries) {
      if (key.toLowerCase() === lower) {
        value = item;
        break;
      }
    }
  }
  if (Array.isArray(value)) return value.join(",");
  return typeof value === "string" ? value : undefined;
}

function payloadBytes(payload: WebhookPayload): Uint8Array {
  if (typeof payload === "string") return encoder.encode(payload);
  if (payload instanceof Uint8Array) return payload;
  if (payload instanceof ArrayBuffer) return new Uint8Array(payload);
  // Частая ошибка — передать уже разобранный JSON (req.body после express.json()).
  throw new TypeError(
    "Webhook payload must be the raw request body (string, Uint8Array/Buffer or ArrayBuffer), not parsed JSON",
  );
}

/** Секрет подписи вебхуков для задач этого API-ключа (без запроса к API). */
export async function webhookSecretFromApiKey(apiKey: string): Promise<string> {
  if (!apiKey) throw new TypeError("apiKey is required");
  return SECRET_PREFIX + (await hmacSha256Hex(apiKey, encoder.encode(DERIVATION_LABEL)));
}

/**
 * Проверяет подпись вебхука. При ошибке бросает WebhookVerificationError.
 *
 * @param payload сырое тело запроса — без повторной сериализации JSON
 *   (в Express: `express.raw({ type: "application/json" })`).
 * @param headers заголовки запроса (регистр не важен).
 * @param secret секрет `whsec_…`.
 */
export async function verifyWebhookSignature(
  payload: WebhookPayload,
  headers: WebhookHeaders,
  secret: string,
  options: VerifyWebhookOptions = {},
): Promise<void> {
  if (!secret) throw new WebhookVerificationError("Webhook secret is empty");
  const timestampRaw = headerValue(headers, TIMESTAMP_HEADER);
  const signatureRaw = headerValue(headers, SIGNATURE_HEADER);
  if (!timestampRaw || !signatureRaw) {
    throw new WebhookVerificationError(`Missing ${TIMESTAMP_HEADER} or ${SIGNATURE_HEADER} header`);
  }
  if (!/^\d+$/.test(timestampRaw.trim())) {
    throw new WebhookVerificationError(`Invalid ${TIMESTAMP_HEADER} header`);
  }
  const timestamp = Number(timestampRaw.trim());
  const tolerance = options.toleranceSeconds ?? DEFAULT_WEBHOOK_TOLERANCE_SECONDS;
  if (!Number.isFinite(tolerance) || tolerance < 0) {
    throw new RangeError("toleranceSeconds must be a non-negative number (0 disables the check)");
  }
  if (tolerance > 0) {
    const now = options.nowSeconds ?? Date.now() / 1000;
    if (Math.abs(now - timestamp) > tolerance) {
      throw new WebhookVerificationError(`Webhook timestamp is outside the tolerance of ${tolerance}s`);
    }
  }
  const body = payloadBytes(payload);
  const prefix = encoder.encode(`${timestamp}.`);
  const signed = new Uint8Array(prefix.length + body.length);
  signed.set(prefix, 0);
  signed.set(body, prefix.length);
  const expected = await hmacSha256Hex(secret, signed);
  for (const part of signatureRaw.split(",")) {
    const eq = part.indexOf("=");
    if (eq === -1) continue;
    const version = part.slice(0, eq).trim();
    const value = part.slice(eq + 1).trim();
    if (version === SIGNATURE_VERSION && timingSafeEqual(value, expected)) return;
  }
  throw new WebhookVerificationError("Webhook signature does not match");
}

/**
 * Проверяет подпись и возвращает событие (`task_id`, `status`, `result`, `error`).
 * @throws WebhookVerificationError подпись не прошла проверку или тело не JSON-объект
 */
export async function unwrapWebhook(
  payload: WebhookPayload,
  headers: WebhookHeaders,
  secret: string,
  options: VerifyWebhookOptions = {},
): Promise<WebhookEvent> {
  await verifyWebhookSignature(payload, headers, secret, options);
  let event: unknown;
  try {
    event = JSON.parse(typeof payload === "string" ? payload : new TextDecoder().decode(payloadBytes(payload)));
  } catch {
    throw new WebhookVerificationError("Webhook body is not valid JSON");
  }
  if (!event || typeof event !== "object" || Array.isArray(event)) {
    throw new WebhookVerificationError("Webhook body is not a JSON object");
  }
  return event as WebhookEvent;
}
