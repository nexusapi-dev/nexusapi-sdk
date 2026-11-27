/**
 * Иерархия исключений SDK. Все наследуются от NexusError —
 * `catch (e) { if (e instanceof NexusError) ... }` ловит всё.
 */

export class NexusError extends Error {
  status?: number;
  responseBody?: unknown;
  /** Заголовки ответа (если ответ был). */
  headers?: Record<string, string>;
  /**
   * Idempotency-Key запроса. Повтор с ним безопасен после NetworkError,
   * ConflictError и ошибок 4xx: если первый запрос выполнился, сервер вернёт
   * его сохранённый ответ. После 5xx сервер ответ не сохраняет — запрос мог
   * быть оплачен, проверьте результат (например, client.tasks.get) прежде чем
   * повторять.
   */
  idempotencyKey?: string;
  /** Задача, к которой относится ошибка (у ошибок ожидания и опроса). */
  taskId?: string;
  /** Объект задачи (Task или объект видео /v1), если он есть. */
  task?: unknown;

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
 * 409 — запрос с этим Idempotency-Key ещё выполняется на сервере. Если 409
 * вызван повтором самого SDK, SDK ждёт до ~2 минут, пока первый запрос
 * закончится. Повторите позже с тем же ключом: когда первый запрос завершится
 * успешно, сервер вернёт его сохранённый ответ.
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
 * Ответ сервера не того формата: не JSON (например, HTML-страница прокси) или
 * без обязательного поля. `.responseBody` — начало тела ответа.
 */
export class APIResponseValidationError extends NexusError {
  constructor(message: string, status?: number, responseBody?: unknown) {
    super(message, status, responseBody);
    this.name = "APIResponseValidationError";
  }
}

/**
 * Task.wait() превысил deadline без завершения задачи (client-side).
 * НЕ то же что серверный 504 — это локальный таймаут polling'а. Задача
 * продолжает идти: `.task` можно дождаться ещё раз, не создавая новую.
 */
export class TaskTimeoutError extends NexusError {
  constructor(message: string, responseBody?: unknown) {
    super(message, undefined, responseBody);
    this.name = "TaskTimeoutError";
  }
}

/**
 * Задача завершилась со status='failed' — провайдер не смог сгенерировать.
 * Деньги уже возвращены на ключ (refund flow в backend). См. `.taskId` и
 * `.responseBody.error`.
 */
export class TaskFailedError extends NexusError {
  constructor(message: string, responseBody?: unknown) {
    super(message, undefined, responseBody);
    this.name = "TaskFailedError";
  }
}

/**
 * Сетевая ошибка (fetch failed, таймаут, ECONNREFUSED, обрыв), после которой
 * SDK больше не повторяет запрос. Исходная причина — в `.cause`.
 *
 * Долгие запросы (чат, v1.images.generate) после обрыва уже отправленного
 * запроса SDK сам не повторяет: генерация могла идти на сервере и быть
 * оплачена. Повторяйте с `idempotencyKey: err.idempotencyKey`, а не с новым
 * ключом: если запрос завершился успешно, сервер вернёт его сохранённый ответ;
 * пока он идёт — ConflictError.
 */
export class NetworkError extends NexusError {
  constructor(message: string, responseBody?: unknown) {
    super(message, undefined, responseBody);
    this.name = "NetworkError";
  }
}
