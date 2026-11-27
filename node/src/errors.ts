/**
 * Иерархия исключений SDK. Все наследуются от NexusError —
 * `catch (e) { if (e instanceof NexusError) ... }` ловит всё.
 *
 * ```
 * NexusError
 * ├── APIStatusError              HTTP-ответ с ошибкой (есть status)
 * │   ├── ValidationError         400 / 422
 * │   ├── AuthenticationError     401 (и нет ключа при создании клиента)
 * │   ├── BillingError            402
 * │   ├── PermissionError         403
 * │   ├── NotFoundError           404
 * │   ├── ConflictError           409
 * │   ├── RateLimitError          429
 * │   └── InternalServerError     5xx
 * ├── NetworkError                ответа нет: соединение, обрыв
 * │   └── APITimeoutError         истёк таймаут запроса
 * ├── APIResponseValidationError  ответ не того формата
 * ├── StreamError                 ошибка внутри SSE-стрима
 * ├── TaskTimeoutError            task.wait() не дождался
 * ├── TaskFailedError             задача завершилась с ошибкой
 * └── WebhookVerificationError    подпись вебхука не прошла проверку
 * ```
 */

export class NexusError extends Error {
  status?: number;
  responseBody?: unknown;
  /** Заголовки ответа (если ответ был). */
  headers?: Record<string, string>;
  /** X-Request-Id ответа — укажите его, когда пишете в поддержку. */
  requestId?: string;
  /** Поля объекта ошибки в формате OpenAI (/v1). */
  code?: string;
  type?: string;
  param?: string;
  /**
   * Idempotency-Key запроса. Генерации (/generate, /v1/images, /v1/videos)
   * сервер привязывает к ключу: повтор с тем же ключом вернёт ту же задачу и
   * не спишет деньги второй раз — даже после 5xx или обрыва. Для чата сервер
   * хранит только успешный ответ: после 5xx повтор вызовет модель снова.
   */
  idempotencyKey?: string;
  /** Задача, к которой относится ошибка (ожидание задачи, 504 у /v1/images). */
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

/**
 * API ответил HTTP-ошибкой. Подклассы — по статусу; прочие статусы (например,
 * 413 или 424) приходят как APIStatusError.
 */
export class APIStatusError extends NexusError {
  constructor(message: string, status?: number, responseBody?: unknown) {
    super(message, status, responseBody);
    this.name = "APIStatusError";
  }
}

export class AuthenticationError extends APIStatusError {
  constructor(message: string, status?: number, responseBody?: unknown) {
    super(message, status, responseBody);
    this.name = "AuthenticationError";
  }
}

export class PermissionError extends APIStatusError {
  constructor(message: string, status?: number, responseBody?: unknown) {
    super(message, status, responseBody);
    this.name = "PermissionError";
  }
}

export class BillingError extends APIStatusError {
  constructor(message: string, status?: number, responseBody?: unknown) {
    super(message, status, responseBody);
    this.name = "BillingError";
  }
}

export class RateLimitError extends APIStatusError {
  constructor(message: string, status?: number, responseBody?: unknown) {
    super(message, status, responseBody);
    this.name = "RateLimitError";
  }
}

export class ValidationError extends APIStatusError {
  constructor(message: string, status?: number, responseBody?: unknown) {
    super(message, status, responseBody);
    this.name = "ValidationError";
  }
}

export class NotFoundError extends APIStatusError {
  constructor(message: string, status?: number, responseBody?: unknown) {
    super(message, status, responseBody);
    this.name = "NotFoundError";
  }
}

/**
 * 409 — запрос с этим Idempotency-Key ещё выполняется на сервере. Если 409
 * вызван повтором самого SDK, SDK ждёт до ~2 минут, пока первый запрос
 * закончится. Повторите позже с тем же ключом: сервер вернёт результат первого
 * запроса.
 */
export class ConflictError extends APIStatusError {
  constructor(message: string, status?: number, responseBody?: unknown) {
    super(message, status, responseBody);
    this.name = "ConflictError";
  }
}

/** 5xx — ошибка на стороне сервера (после повторов SDK). */
export class InternalServerError extends APIStatusError {
  constructor(message: string, status?: number, responseBody?: unknown) {
    super(message, status, responseBody);
    this.name = "InternalServerError";
  }
}

/**
 * Ответа нет: не удалось подключиться или соединение оборвалось, повторы SDK
 * исчерпаны. Исходная причина — в `.cause`. Если запрос успел уйти, повторяйте
 * с тем же `idempotencyKey` — см. NexusError.idempotencyKey.
 */
export class NetworkError extends NexusError {
  constructor(message: string, responseBody?: unknown) {
    super(message, undefined, responseBody);
    this.name = "NetworkError";
  }
}

/** Истёк таймаут запроса (`timeoutMs`). */
export class APITimeoutError extends NetworkError {
  constructor(message: string, responseBody?: unknown) {
    super(message, responseBody);
    this.name = "APITimeoutError";
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
 * Деньги уже возвращены на ключ. См. `.taskId` и `.responseBody.error`.
 */
export class TaskFailedError extends NexusError {
  constructor(message: string, responseBody?: unknown) {
    super(message, undefined, responseBody);
    this.name = "TaskFailedError";
  }
}

/**
 * Подпись вебхука не прошла проверку: нет заголовков, подпись не совпала или
 * запрос слишком старый. Такой запрос нужно отклонить (ответить 400).
 */
export class WebhookVerificationError extends NexusError {
  constructor(message: string) {
    super(message);
    this.name = "WebhookVerificationError";
  }
}
