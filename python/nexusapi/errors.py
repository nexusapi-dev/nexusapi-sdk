"""Иерархия исключений SDK.

Все исключения наследуются от NexusError — `except NexusError` ловит всё.
"""
from typing import Any, Optional


class NexusError(Exception):
    """Базовое исключение. Несёт HTTP-статус и raw-ответ для отладки."""

    def __init__(
        self,
        message: str,
        status_code: Optional[int] = None,
        response_body: Optional[Any] = None,
        idempotency_key: Optional[str] = None,
    ):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.response_body = response_body
        # Idempotency-Key запроса, на котором случилась ошибка (если был).
        # Повтор вызова с этим ключом вернёт результат первого запроса без
        # второго списания, если тот всё-таки выполнился на сервере.
        self.idempotency_key = idempotency_key

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

    Повторите позже с тем же ключом: когда первый запрос закончится, сервер
    вернёт его сохранённый ответ без второго списания.
    """


class StreamError(NexusError):
    """Ошибка, которую сервер прислал внутри SSE-стрима (после 200 OK).

    `.response_body` — событие целиком, например
    `{"error": {"message", "type", "code"}}`.
    """


class TimeoutError(NexusError):  # noqa: A001 — намеренно перекрываем builtin
    """Task.wait() превысил deadline без завершения задачи (client-side)."""


class TaskFailedError(NexusError):
    """Задача завершилась со status='failed' — провайдер не смог сгенерировать.

    Деньги при этом уже возвращены на ключ (refund flow в backend). См.
    `.response_body["task_id"]` и `.response_body["error"]` для деталей.
    """


class NetworkError(NexusError):
    """Сетевая ошибка (connection refused, timeout, обрыв), после которой SDK
    больше не повторяет запрос.

    Долгие запросы (чат, v1.images.generate) после обрыва уже отправленного
    запроса SDK сам не повторяет: генерация могла идти на сервере. Повторите
    вызов с `idempotency_key=err.idempotency_key` — получите её результат без
    второго списания (пока она идёт — ConflictError).
    """
