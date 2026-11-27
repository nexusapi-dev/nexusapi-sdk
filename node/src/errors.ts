/**
 * Иерархия исключений SDK. Все наследуются от NexusError —
 * `catch (e) { if (e instanceof NexusError) ... }` ловит всё.
 */

export class NexusError extends Error {
  status?: number;
  responseBody?: unknown;
  /**
   * Idempotency-Key запроса, на котором случилась ошибка (если был). Повтор
   * вызова с этим ключом вернёт результат первого запроса без второго
   * списания, если тот всё-таки выполнился на сервере.
   */
  idempotencyKey?: string;

  constructor(message: string, status?: number, responseBody?: unknown) {
    super(message);
    this.name = "NexusError";
    this.status = status;
    this.responseBody = responseBody;
    // Required for `instanceof` to work across compile targets / subclasses.
    Object.setPrototypeOf(this, new.target.prototype);
  }
}

export class AuthenticationError extends NexusError {
  constructor(message: string, status?: number, responseBody?: unknown) {
    super(message, status, responseBody);
    this.name = "AuthenticationError";
  }
}

export class PermissionError extends NexusError {
  constructor(message: string, status?: number, responseBody?: unknown) {
    super(message, status, responseBody);
    this.name = "PermissionError";
  }
}

export class BillingError extends NexusError {
  constructor(message: string, status?: number, responseBody?: unknown) {
    super(message, status, responseBody);
    this.name = "BillingError";
  }
}

export class RateLimitError extends NexusError {
  constructor(message: string, status?: number, responseBody?: unknown) {
    super(message, status, responseBody);
    this.name = "RateLimitError";
  }
}

export class ValidationError extends NexusError {
  constructor(message: string, status?: number, responseBody?: unknown) {
    super(message, status, responseBody);
    this.name = "ValidationError";
  }
}

export class NotFoundError extends NexusError {
  constructor(message: string, status?: number, responseBody?: unknown) {
    super(message, status, responseBody);
    this.name = "NotFoundError";
  }
}

/**
 * 409 — запрос с этим Idempotency-Key ещё выполняется на сервере. Повторите
 * позже с тем же ключом: сервер вернёт сохранённый ответ без второго списания.
 */
export class ConflictError extends NexusError {
  constructor(message: string, status?: number, responseBody?: unknown) {
    super(message, status, responseBody);
    this.name = "ConflictError";
  }
}

/**
 * Ошибка, которую сервер прислал внутри SSE-стрима (после 200 OK).
 * `.responseBody` — событие целиком: `{ error: { message, type, code } }`.
 */
export class StreamError extends NexusError {
  constructor(message: string, responseBody?: unknown) {
    super(message, undefined, responseBody);
    this.name = "StreamError";
  }
}

/**
 * Task.wait() превысил deadline без завершения задачи (client-side).
 * НЕ то же что серверный 504 — это локальный таймаут polling'а.
 */
export class TaskTimeoutError extends NexusError {
  constructor(message: string, responseBody?: unknown) {
    super(message, undefined, responseBody);
    this.name = "TaskTimeoutError";
  }
}

/**
 * Задача завершилась со status='failed' — провайдер не смог сгенерировать.
 * Деньги уже возвращены на ключ (refund flow в backend). См. .responseBody
 * для task_id и сообщения провайдера.
 */
export class TaskFailedError extends NexusError {
  constructor(message: string, responseBody?: unknown) {
    super(message, undefined, responseBody);
    this.name = "TaskFailedError";
  }
}

/**
 * Сетевая ошибка (TypeError от fetch, таймаут, ECONNREFUSED, обрыв), после
 * которой SDK больше не повторяет запрос.
 *
 * Долгие запросы (чат, v1.images.generate) после обрыва уже отправленного
 * запроса SDK сам не повторяет: генерация могла идти на сервере. Повторите
 * вызов с `idempotencyKey: err.idempotencyKey` — получите её результат без
 * второго списания (пока она идёт — ConflictError).
 */
export class NetworkError extends NexusError {
  constructor(message: string, responseBody?: unknown) {
    super(message, undefined, responseBody);
    this.name = "NetworkError";
  }
}
