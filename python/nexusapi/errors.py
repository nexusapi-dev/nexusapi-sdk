"""Иерархия исключений SDK.

Все исключения наследуются от NexusError — `except NexusError` ловит всё.
"""
from typing import Any, Mapping, Optional


class NexusError(Exception):
    """Базовое исключение. Несёт HTTP-статус и raw-ответ для отладки.

    Атрибуты (все могут быть None):
        status_code: HTTP-статус ответа.
        response_body: тело ответа (dict, если сервер вернул JSON).
        headers: заголовки ответа.
        idempotency_key: Idempotency-Key запроса. Повтор с ним безопасен после
            NetworkError, ConflictError и ошибок 4xx: если первый запрос
            выполнился, сервер вернёт его сохранённый ответ. После 5xx сервер
            ответ не сохраняет — запрос мог быть оплачен, проверьте результат
            (например, client.tasks.get) прежде чем повторять.
        task_id, task: задача, к которой относится ошибка (у ошибок ожидания).
    """

    def __init__(
        self,
        message: str,
        status_code: Optional[int] = None,
        response_body: Optional[Any] = None,
        idempotency_key: Optional[str] = None,
        headers: Optional[Mapping[str, str]] = None,
    ):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.response_body = response_body
        self.idempotency_key = idempotency_key
        self.headers = headers
        self.task_id: Optional[str] = None
        self.task: Any = None

    def __repr__(self) -> str:
        return (
            f"{self.__class__.__name__}("
            f"message={self.message!r}, status_code={self.status_code!r})"
        )


class AuthenticationError(NexusError):
    """401 — нет ключа, ключ невалидный или отозван."""


class PermissionError(NexusError):
    """403 — restrictions ключа (IP / allowed_models)."""


class BillingError(NexusError):
    """402 — баланса не хватает."""


class RateLimitError(NexusError):
    """429 — превышен rate_limit_per_min ключа."""


class ValidationError(NexusError):
    """400/422 — невалидные параметры запроса."""


class NotFoundError(NexusError):
    """404 — задача / ключ / модель не найдены."""


class ConflictError(NexusError):
    """409 — запрос с этим Idempotency-Key ещё выполняется на сервере.

    Если 409 вызван повтором самого SDK, SDK ждёт до ~2 минут, пока первый
    запрос закончится. Повторите позже с тем же ключом: когда первый запрос
    завершится успешно, сервер вернёт его сохранённый ответ.
    """


class StreamError(NexusError):
    """Ошибка, которую сервер прислал внутри SSE-стрима (после 200 OK).

    `.response_body` — событие целиком, например
    `{"error": {"message", "type", "code"}}`.
    """


class APIResponseValidationError(NexusError):
    """Ответ сервера не того формата: не JSON (например, HTML-страница прокси)
    или без обязательного поля. `.response_body` — начало тела ответа."""


class TimeoutError(NexusError):  # noqa: A001 — намеренно перекрываем builtin
    """Task.wait() превысил deadline без завершения задачи (client-side).

    Задача при этом продолжает выполняться: `.task_id` / `.task` — чтобы
    дождаться её позже, а не создавать (и оплачивать) новую.
    """


class TaskFailedError(NexusError):
    """Задача завершилась со status='failed' — провайдер не смог сгенерировать.

    Деньги при этом уже возвращены на ключ (refund flow в backend). См.
    `.task_id` и `.response_body["error"]` для деталей.
    """


class NetworkError(NexusError):
    """Сетевая ошибка (connection refused, timeout, обрыв), после которой SDK
    больше не повторяет запрос.

    Долгие запросы (чат, v1.images.generate) после обрыва уже отправленного
    запроса SDK сам не повторяет: генерация могла идти на сервере и быть
    оплачена. Повторяйте с `idempotency_key=err.idempotency_key`, а не с новым
    ключом: если запрос завершился успешно, сервер вернёт его сохранённый
    ответ; пока он идёт — ConflictError.
    """
