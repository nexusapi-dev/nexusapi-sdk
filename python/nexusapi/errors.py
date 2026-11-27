"""Иерархия исключений SDK.

Все исключения наследуются от NexusError — `except NexusError` ловит всё.

    NexusError
    ├── APIStatusError              HTTP-ответ с ошибкой (есть status_code)
    │   ├── ValidationError         400 / 422
    │   ├── AuthenticationError     401 (и нет ключа при создании клиента)
    │   ├── BillingError            402
    │   ├── PermissionError         403 (экспортируется как NexusPermissionError)
    │   ├── NotFoundError           404
    │   ├── ConflictError           409
    │   ├── RateLimitError          429
    │   └── InternalServerError     5xx
    ├── NetworkError                ответа нет: соединение, обрыв
    │   └── APITimeoutError         истёк таймаут запроса
    ├── APIResponseValidationError  ответ не того формата
    ├── StreamError                 ошибка внутри SSE-стрима
    ├── TimeoutError                task.wait() не дождался (NexusTimeoutError)
    ├── TaskFailedError             задача завершилась с ошибкой
    └── WebhookVerificationError    подпись вебхука не прошла проверку
"""
from typing import Any, Mapping, Optional


class NexusError(Exception):
    """Базовое исключение SDK.

    Атрибуты (все могут быть None):
        status_code: HTTP-статус ответа.
        response_body: тело ответа (dict, если сервер вернул JSON).
        headers: заголовки ответа.
        request_id: X-Request-Id ответа — укажите его, когда пишете в поддержку.
        code, type, param: поля объекта ошибки в формате OpenAI (/v1).
        idempotency_key: Idempotency-Key запроса. Генерации (/generate,
            /v1/images, /v1/videos) сервер привязывает к ключу: повтор с тем же
            ключом вернёт ту же задачу и не спишет деньги второй раз — даже
            после 5xx или обрыва. Для чата сервер хранит только успешный ответ:
            после 5xx повтор вызовет модель снова.
        task_id, task: задача, к которой относится ошибка (ожидание задачи,
            504 у /v1/images).
    """

    def __init__(
        self,
        message: str,
        status_code: Optional[int] = None,
        response_body: Optional[Any] = None,
        idempotency_key: Optional[str] = None,
        headers: Optional[Mapping[str, str]] = None,
        request_id: Optional[str] = None,
    ):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.response_body = response_body
        self.idempotency_key = idempotency_key
        self.headers = headers
        self.request_id = request_id
        self.code: Optional[str] = None
        self.type: Optional[str] = None
        self.param: Optional[str] = None
        self.task_id: Optional[str] = None
        self.task: Any = None

    def __repr__(self) -> str:
        parts = [f"message={self.message!r}", f"status_code={self.status_code!r}"]
        if self.request_id:
            parts.append(f"request_id={self.request_id!r}")
        return f"{self.__class__.__name__}({', '.join(parts)})"


class APIStatusError(NexusError):
    """API ответил HTTP-ошибкой. Подклассы — по статусу; прочие статусы
    (например, 413 или 424) приходят как APIStatusError."""


class AuthenticationError(APIStatusError):
    """401 — ключ невалидный или отозван (или ключа нет при создании клиента)."""


class PermissionError(APIStatusError):  # noqa: A001 — экспортируется как NexusPermissionError
    """403 — ограничения ключа (IP / allowed_models)."""


class BillingError(APIStatusError):
    """402 — баланса не хватает."""


class RateLimitError(APIStatusError):
    """429 — превышен rate_limit_per_min ключа."""


class ValidationError(APIStatusError):
    """400/422 — невалидные параметры запроса."""


class NotFoundError(APIStatusError):
    """404 — задача / ключ / модель не найдены."""


class ConflictError(APIStatusError):
    """409 — запрос с этим Idempotency-Key ещё выполняется на сервере.

    Если 409 вызван повтором самого SDK, SDK ждёт до ~2 минут, пока первый
    запрос закончится. Повторите позже с тем же ключом: сервер вернёт результат
    первого запроса.
    """


class InternalServerError(APIStatusError):
    """5xx — ошибка на стороне сервера (после повторов SDK)."""


class NetworkError(NexusError):
    """Ответа нет: не удалось подключиться или соединение оборвалось, повторы
    SDK исчерпаны. Если запрос успел уйти, повторяйте с тем же
    `idempotency_key` — см. NexusError.idempotency_key."""


class APITimeoutError(NetworkError):
    """Истёк таймаут запроса (подключения или ожидания ответа)."""


class APIResponseValidationError(NexusError):
    """Ответ сервера не того формата: не JSON (например, HTML-страница прокси)
    или без обязательного поля. `.response_body` — начало тела ответа."""


class StreamError(NexusError):
    """Ошибка, которую сервер прислал внутри SSE-стрима (после 200 OK).

    `.response_body` — событие целиком, например
    `{"error": {"message", "type", "code"}}`.
    """


class TimeoutError(NexusError):  # noqa: A001 — экспортируется как NexusTimeoutError
    """Task.wait() превысил deadline без завершения задачи (client-side).

    Задача при этом продолжает выполняться: `.task_id` / `.task` — чтобы
    дождаться её позже, а не создавать (и оплачивать) новую.
    """


class TaskFailedError(NexusError):
    """Задача завершилась со status='failed' — провайдер не смог сгенерировать.

    Деньги при этом уже возвращены на ключ. См. `.task_id` и
    `.response_body["error"]`.
    """


class WebhookVerificationError(NexusError):
    """Подпись вебхука не прошла проверку: нет заголовков, подпись не совпала
    или запрос слишком старый. Такой запрос нужно отклонить (ответить 400)."""
