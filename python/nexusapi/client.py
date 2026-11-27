"""Sync (NexusClient) и async (AsyncNexusClient) клиенты NexusAPI.

Публичный API одинаков, методы идентичны по сигнатурам — отличается только
блокирующий vs `await`. Дизайн вдохновлён OpenAI/Stripe-стилем:
`client.<resource>.<verb>(...)`. Без магии и метапрограммирования.

Ресурсы:
- нативный API: `videos`, `images`, `tasks`, `models` (`POST /generate`,
  `GET /tasks/{id}`, `GET /public/models`);
- OpenAI-совместимый `/v1`: `chat.completions` (в том числе SSE-стрим) и
  `v1.models`, `v1.images`, `v1.videos`. `client.v1.chat` — тот же объект,
  что и `client.chat`.

Reliability:
- **Retries on 5xx + 429 + network errors** — до `max_retries` (дефолт 2) с
  экспоненциальным backoff (0.5s, 1s, 2s + jitter).
- **Auto Idempotency-Key** — на каждом POST автоматически генерируется UUID v4
  (если клиент не передал свой). Гарантирует что retry не создаст дубль задачи
  и второго списания. Исключение — стрим чата: ключ уходит только если его
  передали явно, а повтор делается только когда запрос не дошёл до сервера.

Sync пример:
    from nexusapi import NexusClient
    with NexusClient(api_key="...") as client:
        task = client.videos.create(model="veo-3-fast", prompt="a cat", duration=8)
        task.wait()
        print(task.video_url)

        reply = client.chat.completions.create(
            model="claude-sonnet-5",
            messages=[{"role": "user", "content": "Привет"}],
        )
        print(reply["choices"][0]["message"]["content"])

Async пример:
    import asyncio
    from nexusapi import AsyncNexusClient

    async def main():
        async with AsyncNexusClient(api_key="...") as client:
            task = await client.videos.create(model="veo-3-fast", prompt="a cat", duration=8)
            await task.wait()
            print(task.video_url)

    asyncio.run(main())
"""
from __future__ import annotations

import asyncio
import email.utils
import json as _json
import logging
import math
import os
import platform
import random
import re
import time
import uuid
import warnings
from dataclasses import dataclass, field
from typing import (
    Any,
    AsyncIterator,
    Iterator,
    Literal,
    Mapping,
    Optional,
    Type,
    Union,
    cast,
    overload,
)
from urllib.parse import quote, urlsplit

import httpx

from . import errors
from . import webhooks as _webhooks
from .types import (
    ChatCompletion,
    ChatCompletionChunk,
    ImagesResponse,
    ModelInfo,
    OpenAIModel,
    Video,
    WebhookEvent,
)

DEFAULT_BASE_URL = "https://nexusapi.dev"
#: Переменные окружения, из которых клиент берёт ключ и адрес API.
ENV_API_KEY = "NEXUSAPI_API_KEY"
ENV_BASE_URL = "NEXUSAPI_BASE_URL"
DEFAULT_TIMEOUT = 60.0
DEFAULT_MAX_RETRIES = 2

# Default poll intervals for Task.wait — короче для image-моделей (~5-15s),
# длиннее для video (минуты).
DEFAULT_VIDEO_POLL_INTERVAL = 3.0
DEFAULT_IMAGE_POLL_INTERVAL = 1.5
DEFAULT_VIDEO_WAIT_TIMEOUT = 600.0  # 10 min
# Картинки обычно готовы за 10-60 с, но Seedream Pro идёт до 540 с.
DEFAULT_IMAGE_WAIT_TIMEOUT = 600.0  # 10 min

# Синхронные /v1-запросы (чат, /v1/images/generations) сервер держит долго:
# картинки до 180 с (Seedream Pro до 540 с), ответ LLM — минуты. Таймаут
# чтения для них — не меньше 600 с (лимит прокси API), подключение — по
# обычному таймауту клиента.
DEFAULT_LONG_REQUEST_TIMEOUT = 600.0

# POST /generate у части моделей до ответа проверяет входные файлы (до 90 с).
DEFAULT_GENERATE_TIMEOUT = 120.0

# Больше Retry-After не ждём: вместо часового сна — backoff.
_MAX_RETRY_AFTER = 60.0
# Сколько ждать, пока сервер закончит первый запрос, если повтор самого SDK
# получил 409 «ещё выполняется».
_CONFLICT_WAIT_BUDGET = 120.0
_MAX_ERROR_MESSAGE = 500

logger = logging.getLogger("nexusapi")

# Статусы которые retry'им. 429 — rate limit (даём время отпустить),
# 5xx — серверная нестабильность которая может пройти сама.
_RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})

# Генерации (/generate, /v1/images, /v1/videos) сервер привязывает к
# Idempotency-Key: повтор после 5xx или обрыва возвращает ту же задачу, а у
# /v1/images 504 повтор продолжает ждать её. Поэтому их повторяем как обычно.
#
# Чат к задаче не привязан: сервер хранит только успешный ответ, а 500 мог
# прийти после вызова модели — его не повторяем. 502/504 приходят от прокси,
# пока сервер ещё работает: повтор с тем же ключом получит 409 (SDK подождёт)
# или сохранённый ответ.
_CHAT_RETRY_STATUSES = frozenset({429, 502, 503, 504})

# SSE-стрим сервер не сохраняет вовсе: повторяем только ответы, пришедшие до
# начала генерации.
_SAFE_RETRY_STATUSES = frozenset({429, 503})

# Ошибки, при которых запрос гарантированно не ушёл на сервер. Только их
# повторяем для SSE-стрима: стрим нельзя переотправить с середины.
_NOT_SENT_ERRORS = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)



def _get_version() -> str:
    """Версия пакета из installed metadata. Используется в User-Agent."""
    try:
        from importlib.metadata import PackageNotFoundError, version
        try:
            return version("nexusapi-sdk")
        except PackageNotFoundError:
            pass
    except Exception:
        pass
    return "0.0.0+unknown"


VERSION = _get_version()
_USER_AGENT = f"nexusapi-python/{VERSION} python/{platform.python_version()}"


class NotGiven:
    """Значение «не передано» для with_options() (в отличие от None)."""

    def __bool__(self) -> bool:
        return False

    def __repr__(self) -> str:
        return "NOT_GIVEN"


NOT_GIVEN = NotGiven()


def _backoff_delay(attempt: int) -> float:
    """Exponential backoff с jitter. attempt начинается с 0.

    0 → 0.5..0.625s, 1 → 1..1.25s, 2 → 2..2.5s, 3+ → 4..5s (jitter до +25%).
    """
    base: float = min(4.0, 0.5 * (2 ** attempt))
    return base * random.uniform(1.0, 1.25)


def _truncate(text: str, limit: int = _MAX_ERROR_MESSAGE) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[:limit] + "…"


def _error_message(body: Any, text: str, status_code: int) -> str:
    """Сообщение об ошибке из тела ответа любой формы, не длиннее 500 символов."""
    message: Any = None
    if isinstance(body, dict):
        err = body.get("error")
        detail = body.get("detail")
        if isinstance(err, dict):
            message = err.get("message")
        elif isinstance(err, str):
            message = err
        if not message and isinstance(detail, str):
            message = detail
        elif not message and isinstance(detail, list) and detail:
            first = detail[0]
            if isinstance(first, dict):
                # Pydantic-валидация: [{"loc": [...], "msg": ...}]
                loc = ".".join(str(x) for x in (first.get("loc") or [])[1:]) or "request"
                message = f"{loc}: {first.get('msg') or 'invalid'}"
            else:
                message = str(first)
        elif not message and isinstance(detail, dict):
            message = detail.get("message") or detail.get("msg") or _json.dumps(
                detail, ensure_ascii=False,
            )
    elif text:
        message = text
    return _truncate(str(message)) if message else f"HTTP {status_code}"


def _parse_error(resp: httpx.Response) -> tuple[str, Type[errors.APIStatusError], Any]:
    """Парсит response → (message, exception_class, response_body).

    Используется в обоих sync/async вариантах _raise_for_status — extract
    в отдельный helper потому что respond_status маппинг и message-extraction
    одинаковы.
    """
    text = ""
    try:
        text = resp.text
    except Exception:  # битая кодировка — сообщение будет "HTTP <код>"
        text = ""
    body: Any = None
    try:
        body = _json.loads(text) if text else None
    except ValueError:
        body = _truncate(text, 2000) if text else None
    message = _error_message(body, text, resp.status_code)

    cls_map: dict[int, Type[errors.APIStatusError]] = {
        400: errors.ValidationError,
        401: errors.AuthenticationError,
        402: errors.BillingError,
        403: errors.PermissionError,
        404: errors.NotFoundError,
        409: errors.ConflictError,
        422: errors.ValidationError,
        429: errors.RateLimitError,
    }
    cls = cls_map.get(resp.status_code)
    if cls is None:
        cls = errors.InternalServerError if resp.status_code >= 500 else errors.APIStatusError
    return message, cls, body


def _parse_retry_after(headers: Mapping[str, str]) -> Optional[float]:
    """Retry-After в секундах: `retry-after-ms`, `retry-after` (секунды или
    HTTP-дата). None, если заголовка нет или значение негодное (отрицательное,
    не число, больше _MAX_RETRY_AFTER)."""
    value: Optional[float] = None
    raw_ms = headers.get("retry-after-ms")
    raw = headers.get("retry-after")
    try:
        if raw_ms:
            value = float(raw_ms) / 1000.0
        elif raw:
            try:
                value = float(raw)
            except ValueError:
                when = email.utils.parsedate_to_datetime(raw)
                value = when.timestamp() - time.time()
    except (TypeError, ValueError, OverflowError):
        return None
    if value is None or not math.isfinite(value) or value < 0 or value > _MAX_RETRY_AFTER:
        return None
    return value


def _retry_delay(resp: httpx.Response, attempt: int) -> float:
    """Задержка перед повтором: Retry-After, если он разумный, иначе backoff."""
    retry_after = _parse_retry_after(resp.headers)
    return retry_after if retry_after is not None else _backoff_delay(attempt)


# ============================================================
# SSE (text/event-stream) для chat.completions(stream=True)
# ============================================================

_LINE_BREAK = re.compile(r"\r\n|\r|\n")


class _SSEDecoder:
    """Разбирает поток текста в payload'ы `data:` событий SSE.

    Свой разбор вместо `iter_lines()`, чтобы не зависеть от того, как разные
    версии httpx отдают пустые строки (а пустая строка — граница события).
    """

    def __init__(self) -> None:
        self._pending = ""
        self._data: list[str] = []

    def feed(self, chunk: str) -> list[str]:
        text = self._pending + chunk
        # '\r' в конце может оказаться первой половиной '\r\n' — ждём следующий кусок.
        hold_cr = text.endswith("\r")
        if hold_cr:
            text = text[:-1]
        lines = _LINE_BREAK.split(text)
        self._pending = lines.pop() + ("\r" if hold_cr else "")
        events: list[str] = []
        for line in lines:
            event = self._line(line)
            if event is not None:
                events.append(event)
        return events

    def flush(self) -> list[str]:
        events: list[str] = []
        if self._pending:
            event = self._line(self._pending.rstrip("\r"))
            self._pending = ""
            if event is not None:
                events.append(event)
        if self._data:
            events.append("\n".join(self._data))
            self._data = []
        return events

    def _line(self, line: str) -> Optional[str]:
        if line == "":
            if not self._data:
                return None
            event = "\n".join(self._data)
            self._data = []
            return event
        if line.startswith(":"):
            return None  # комментарий / keep-alive
        name, _, value = line.partition(":")
        if value.startswith(" "):
            value = value[1:]
        if name == "data":
            self._data.append(value)
        return None


_STREAM_DONE = object()


def _decode_stream_event(payload: str) -> Any:
    """`[DONE]` → _STREAM_DONE; JSON-chunk → dict; ошибка внутри стрима → StreamError."""
    if payload.strip() == "[DONE]":
        return _STREAM_DONE
    try:
        event = _json.loads(payload)
    except ValueError as e:
        raise errors.StreamError(
            f"Malformed stream event: {payload[:200]!r}", response_body=payload,
        ) from e
    if isinstance(event, dict) and isinstance(event.get("error"), dict):
        raise errors.StreamError(
            event["error"].get("message") or "Stream error", response_body=event,
        )
    return event


class Stream:
    """SSE-стрим `chat.completion.chunk`. Итерируется по dict-чанкам.

        with client.chat.completions.create(..., stream=True) as stream:
            for chunk in stream:
                delta = chunk["choices"][0]["delta"] if chunk["choices"] else {}
                print(delta.get("content") or "", end="")

    Соединение закрывается, когда стрим дочитан, при выходе из `with` или
    при `close()`. Обрыв посреди стрима — NetworkError (без повтора);
    ошибка, присланная сервером внутри стрима, — StreamError.
    """

    def __init__(self, response: httpx.Response):
        self.response = response
        self._iterator = self._iter_events()

    def _iter_events(self) -> Iterator[ChatCompletionChunk]:
        decoder = _SSEDecoder()
        try:
            try:
                for chunk in self.response.iter_text():
                    for payload in decoder.feed(chunk):
                        event = _decode_stream_event(payload)
                        if event is _STREAM_DONE:
                            return
                        yield event
                for payload in decoder.flush():
                    event = _decode_stream_event(payload)
                    if event is _STREAM_DONE:
                        return
                    yield event
            except httpx.TransportError as e:
                raise errors.NetworkError(f"Stream interrupted: {e}") from e
        finally:
            self.response.close()

    def __iter__(self) -> Iterator[ChatCompletionChunk]:
        return self._iterator

    def __next__(self) -> ChatCompletionChunk:
        return next(self._iterator)

    def close(self) -> None:
        self.response.close()

    def __enter__(self) -> "Stream":
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()


class AsyncStream:
    """Async-вариант Stream: `async for chunk in stream`."""

    def __init__(self, response: httpx.Response):
        self.response = response
        self._iterator = self._iter_events()

    async def _iter_events(self) -> AsyncIterator[ChatCompletionChunk]:
        decoder = _SSEDecoder()
        try:
            try:
                async for chunk in self.response.aiter_text():
                    for payload in decoder.feed(chunk):
                        event = _decode_stream_event(payload)
                        if event is _STREAM_DONE:
                            return
                        yield event
                for payload in decoder.flush():
                    event = _decode_stream_event(payload)
                    if event is _STREAM_DONE:
                        return
                    yield event
            except httpx.TransportError as e:
                raise errors.NetworkError(f"Stream interrupted: {e}") from e
        finally:
            await self.response.aclose()

    def __aiter__(self) -> AsyncIterator[ChatCompletionChunk]:
        return self._iterator

    async def __anext__(self) -> ChatCompletionChunk:
        return await self._iterator.__anext__()

    async def close(self) -> None:
        await self.response.aclose()

    async def __aenter__(self) -> "AsyncStream":
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.close()


# ============================================================
# Task (sync) + AsyncTask
# ============================================================


@dataclass
class _TaskBase:
    """Общие поля + read-only properties для sync/async Task."""
    id: str
    status: str
    model: Optional[str] = None
    result: Optional[Any] = None
    error: Optional[str] = None
    created_at: Optional[str] = None

    @property
    def is_done(self) -> bool:
        return self.status in ("completed", "failed")

    def __getstate__(self) -> dict[str, Any]:
        # Без клиента: в нём HTTP-пул и ключ API. После unpickle задачу можно
        # прочитать, а для refresh()/wait() — получить заново через tasks.get().
        state = self.__dict__.copy()
        state["_client"] = None
        return state

    def _result_dict(self) -> dict[str, Any]:
        return self.result if isinstance(self.result, dict) else {}

    @property
    def video_url(self) -> Optional[str]:
        return _str_or_none(self._result_dict().get("video_url"))

    @property
    def image_url(self) -> Optional[str]:
        """Берёт image_urls[0] (для моделей возвращающих список) или image_url."""
        r = self._result_dict()
        urls = r.get("image_urls")
        if isinstance(urls, list) and urls:
            return _str_or_none(urls[0])
        return _str_or_none(r.get("image_url"))

    @property
    def image_urls(self) -> list[str]:
        urls = self._result_dict().get("image_urls")
        return list(urls) if isinstance(urls, list) else []

    @property
    def audio_url(self) -> Optional[str]:
        """Берёт audio_urls[0] (Suno и др.) или audio_url."""
        r = self._result_dict()
        urls = r.get("audio_urls")
        if isinstance(urls, list) and urls:
            return _str_or_none(urls[0])
        return _str_or_none(r.get("audio_url"))

    @property
    def audio_urls(self) -> list[str]:
        urls = self._result_dict().get("audio_urls")
        return list(urls) if isinstance(urls, list) else []

    def _timeout_error(self, timeout: float) -> errors.TimeoutError:
        err = errors.TimeoutError(
            f"Task {self.id} did not complete within {timeout}s "
            f"(last status: {self.status}); it is still running — wait for it again, "
            f"do not create a new one",
            response_body={"task_id": self.id, "last_status": self.status},
        )
        err.task_id, err.task = self.id, self
        return err

    def _failed_error(self) -> errors.TaskFailedError:
        err = errors.TaskFailedError(
            f"Task {self.id} failed: {self.error or 'unknown error'}",
            response_body={"task_id": self.id, "error": self.error},
        )
        err.task_id, err.task = self.id, self
        return err


def _str_or_none(value: Any) -> Optional[str]:
    return value if isinstance(value, str) else None


def _is_transient(e: errors.NexusError) -> bool:
    """Ошибка опроса, которую стоит переждать, а не прерывать ожидание задачи."""
    if isinstance(e, errors.NetworkError):
        return True
    return e.status_code is not None and (e.status_code == 429 or e.status_code >= 500)


@dataclass
class Task(_TaskBase):
    """Sync Task. Возвращается всеми create/generate-методами NexusClient."""

    # Приватная ссылка на клиент для refresh()/wait(). field(compare=False)
    # чтобы dataclass __eq__ не учитывал client при сравнении тасков.
    _client: Optional["NexusClient"] = field(default=None, repr=False, compare=False)

    def refresh(self) -> "Task":
        """Перезапрашивает статус. Возвращает self для chain'инга."""
        if self._client is None:
            raise errors.NexusError("Task is not bound to a client — cannot refresh")
        fresh = self._client.tasks.get(self.id)
        self.status = fresh.status
        self.result = fresh.result
        self.error = fresh.error
        self.created_at = fresh.created_at or self.created_at
        self.model = fresh.model or self.model
        return self

    def wait(
        self,
        timeout: float = DEFAULT_VIDEO_WAIT_TIMEOUT,
        poll_interval: float = DEFAULT_VIDEO_POLL_INTERVAL,
    ) -> "Task":
        """Блокируется до завершения задачи.

        Временные ошибки опроса (сеть, 429, 5xx) пережидает до дедлайна.

        Raises:
            NexusTimeoutError: задача не завершилась за `timeout` секунд; она
                продолжает идти — `.task` можно дождаться ещё раз.
            TaskFailedError: задача завершилась со status='failed'.
        """
        deadline = time.monotonic() + timeout
        while not self.is_done:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise self._timeout_error(timeout)
            time.sleep(min(poll_interval, remaining))
            try:
                self.refresh()
            except errors.NexusError as e:
                if not _is_transient(e) or time.monotonic() >= deadline:
                    e.task_id, e.task = self.id, self
                    raise
                logger.info("Polling task %s failed (%s), will retry", self.id, e)
        if self.status == "failed":
            raise self._failed_error()
        return self


@dataclass
class AsyncTask(_TaskBase):
    """Async Task. Возвращается всеми create/generate-методами AsyncNexusClient.

    Идентичен Task по полям/properties, но `refresh()` и `wait()` — async.
    """

    _client: Optional["AsyncNexusClient"] = field(default=None, repr=False, compare=False)

    async def refresh(self) -> "AsyncTask":
        if self._client is None:
            raise errors.NexusError("Task is not bound to a client — cannot refresh")
        fresh = await self._client.tasks.get(self.id)
        self.status = fresh.status
        self.result = fresh.result
        self.error = fresh.error
        self.created_at = fresh.created_at or self.created_at
        self.model = fresh.model or self.model
        return self

    async def wait(
        self,
        timeout: float = DEFAULT_VIDEO_WAIT_TIMEOUT,
        poll_interval: float = DEFAULT_VIDEO_POLL_INTERVAL,
    ) -> "AsyncTask":
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while not self.is_done:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise self._timeout_error(timeout)
            await asyncio.sleep(min(poll_interval, remaining))
            try:
                await self.refresh()
            except errors.NexusError as e:
                if not _is_transient(e) or loop.time() >= deadline:
                    e.task_id, e.task = self.id, self
                    raise
                logger.info("Polling task %s failed (%s), will retry", self.id, e)
        if self.status == "failed":
            raise self._failed_error()
        return self


# ============================================================
# Параметры для resources (общие helpers)
# ============================================================


def _build_generate_params(
    *,
    model: str,
    prompt: str,
    duration: Optional[int] = None,
    aspect_ratio: Optional[str] = None,
    extras: dict[str, Any],
) -> dict[str, Any]:
    """Собирает params для POST /generate из именованных + extras kwargs."""
    params: dict[str, Any] = {"model_name": model, "prompt": prompt}
    if duration is not None:
        params["duration"] = duration
    if aspect_ratio is not None:
        params["aspect_ratio"] = aspect_ratio
    # extras — все native-параметры (webhook_url, negative_prompt, image_url, ...).
    # Фильтруем None чтобы не слать "image_url": null если юзер просто не передал.
    for k, v in extras.items():
        if v is not None:
            params[k] = v
    return params


def _generate_idempotency_key(user_key: Optional[str]) -> str:
    """Возвращает user_key если задан, иначе генерит свой UUID v4.

    `sdk-` префикс помогает отличать в логах что ключ автогенерирован клиентом
    (а не передан юзером).
    """
    if user_key:
        _validate_idempotency_key(user_key)
        return user_key
    return f"sdk-{uuid.uuid4()}"


def _validate_idempotency_key(key: str) -> None:
    """Сервер принимает ключи 8–256 символов; проверяем до отправки."""
    if not 8 <= len(key) <= 256:
        raise ValueError("idempotency_key must be 8-256 characters long")


def _task_path(task_id: str) -> str:
    if not task_id:
        raise ValueError("task_id is required")
    return f"/tasks/{quote(str(task_id), safe='')}"


def _require_task_id(data: Any, idem_key: Optional[str] = None) -> str:
    task_id = data.get("task_id") if isinstance(data, dict) else None
    if not isinstance(task_id, str) or not task_id:
        raise errors.APIResponseValidationError(
            "Server response has no task_id",
            response_body=data, idempotency_key=idem_key,
        )
    return task_id


def _task_kwargs(data: Any) -> dict[str, Any]:
    """Поля Task из ответа GET /tasks/{id}."""
    task_id = _require_task_id(data)
    status = data.get("status")
    if not isinstance(status, str):
        raise errors.APIResponseValidationError(
            "Server response has no task status", response_body=data,
        )
    return {
        "id": task_id, "status": status, "model": None,
        "result": data.get("result"), "error": data.get("error"),
        "created_at": data.get("created_at"),
    }


def _build_body(required: dict[str, Any], optional: dict[str, Any]) -> dict[str, Any]:
    """Тело /v1-запроса: обязательные поля + опциональные без None."""
    body = dict(required)
    for k, v in optional.items():
        if v is not None:
            body[k] = v
    return body


def _video_id(video: Union[str, Mapping[str, Any]]) -> str:
    video_id = video.get("id") if isinstance(video, dict) else video
    if not video_id:
        raise ValueError("video id is required")
    return str(video_id)


def _video_path(video: Union[str, Mapping[str, Any]]) -> str:
    return f"/v1/videos/{quote(_video_id(video), safe='')}"


def _raise_if_video_failed(video: Mapping[str, Any]) -> None:
    if video.get("status") != "failed":
        return
    err = video.get("error") or {}
    message = err.get("message") if isinstance(err, dict) else None
    err = errors.TaskFailedError(
        f"Video {video.get('id')} failed: {message or 'unknown error'}",
        response_body={"task_id": video.get("id"), "error": message, "video": video},
    )
    err.task_id, err.task = video.get("id"), video
    raise err


def _video_timeout_error(current: Mapping[str, Any], timeout: float) -> errors.TimeoutError:
    err = errors.TimeoutError(
        f"Video {current.get('id')} did not complete within {timeout}s "
        f"(last status: {current.get('status')}); it is still running — wait for it "
        f"again, do not create a new one",
        response_body={"task_id": current.get("id"), "last_status": current.get("status")},
    )
    err.task_id, err.task = current.get("id"), current
    return err


_VIDEO_DONE_STATUSES = ("completed", "failed")


# ============================================================
# Sync resources
# ============================================================


class _Resource:
    """Общий базовый класс для resources — даёт доступ к client."""
    def __init__(self, client: "NexusClient"):
        self._client = client


class Videos(_Resource):
    """Видео — async-only по UX (на сервере): create() → Task → task.wait()."""

    def create(
        self,
        *,
        model: str,
        prompt: str,
        duration: Optional[int] = None,
        aspect_ratio: Optional[str] = None,
        idempotency_key: Optional[str] = None,
        **extras: Any,
    ) -> Task:
        """Создаёт видео-задачу. Возвращает Task в статусе pending.

        Args:
            model: ID модели (например, "veo-3-fast"). См. client.models.list().
            prompt: Текстовое описание.
            duration: Длительность в секундах. None — дефолт модели.
            aspect_ratio: "16:9" / "9:16" / "1:1" / etc. None — дефолт модели.
            idempotency_key: Опционально. Если передан — retry с тем же ключом
                вернёт тот же task_id без второго списания. Если None — SDK
                сам сгенерит UUID, и retry в случае сбоя останутся безопасны.
                См. docs.nexusapi.dev/concepts/idempotency.
            **extras: Любые native-параметры модели (webhook_url, negative_prompt,
                image_url для image-to-video, и т.д.) — передаются прямо в API.
        """
        params = _build_generate_params(
            model=model, prompt=prompt, duration=duration,
            aspect_ratio=aspect_ratio, extras=extras,
        )
        headers = {"Idempotency-Key": _generate_idempotency_key(idempotency_key)}
        data = self._client._request(
            "POST", "/generate", json={"params": params}, extra_headers=headers,
            timeout=self._client._generate_timeout(),
        )
        return Task(
            id=_require_task_id(data, headers["Idempotency-Key"]),
            status="pending",
            model=model,
            _client=self._client,
        )


class Images(_Resource):
    """Изображения. По умолчанию generate() ждёт завершения; wait=False вернёт
    Task в pending для ручного polling'а."""

    def generate(
        self,
        *,
        model: str,
        prompt: str,
        aspect_ratio: Optional[str] = None,
        wait: bool = True,
        timeout: float = DEFAULT_IMAGE_WAIT_TIMEOUT,
        poll_interval: float = DEFAULT_IMAGE_POLL_INTERVAL,
        idempotency_key: Optional[str] = None,
        **extras: Any,
    ) -> Task:
        """Создаёт image-задачу и (по умолчанию) ждёт завершения."""
        params = _build_generate_params(
            model=model, prompt=prompt, aspect_ratio=aspect_ratio, extras=extras,
        )
        headers = {"Idempotency-Key": _generate_idempotency_key(idempotency_key)}
        data = self._client._request(
            "POST", "/generate", json={"params": params}, extra_headers=headers,
            timeout=self._client._generate_timeout(),
        )
        task = Task(
            id=_require_task_id(data, headers["Idempotency-Key"]),
            status="pending", model=model, _client=self._client,
        )
        if wait:
            task.wait(timeout=timeout, poll_interval=poll_interval)
        return task


class Tasks(_Resource):
    def get(self, task_id: str) -> Task:
        data = self._client._request("GET", _task_path(task_id))
        return Task(**_task_kwargs(data), _client=self._client)


class Models(_Resource):
    def list(self) -> list[ModelInfo]:
        """Возвращает каталог моделей с ценами. Использует публичный
        /public/models (без ключа). Список в OpenAI-формате — client.v1.models.list()."""
        data = self._client._request("GET", "/public/models", auth=False)
        return list(data or [])


class ChatCompletions(_Resource):
    """POST /v1/chat/completions — текстовые модели (Claude, GPT) в формате OpenAI."""

    @overload
    def create(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        stream: Literal[False] = False,
        idempotency_key: Optional[str] = None,
        timeout: Optional[float] = None,
        **extras: Any,
    ) -> ChatCompletion: ...

    @overload
    def create(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        stream: Literal[True],
        idempotency_key: Optional[str] = None,
        timeout: Optional[float] = None,
        **extras: Any,
    ) -> Stream: ...

    @overload
    def create(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        stream: bool,
        idempotency_key: Optional[str] = None,
        timeout: Optional[float] = None,
        **extras: Any,
    ) -> Union[ChatCompletion, Stream]: ...

    def create(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        stream: bool = False,
        idempotency_key: Optional[str] = None,
        timeout: Optional[float] = None,
        **extras: Any,
    ) -> Union[ChatCompletion, Stream]:
        """Ответ модели. Без stream — dict в формате OpenAI `chat.completion`
        (`reply["choices"][0]["message"]["content"]`, `reply["usage"]`).
        С `stream=True` — Stream с чанками `chat.completion.chunk`.

        Args:
            model: например "claude-sonnet-5", "gpt-6-luna". Список —
                client.v1.models.list() или docs.nexusapi.dev/models/llm.
            messages: сообщения OpenAI Chat Completions (в т.ч. блоки
                image_url для моделей с Vision).
            stream: True — SSE-стрим.
            idempotency_key: без стрима SDK генерирует ключ сам, и повтор
                после сбоя вернёт тот же ответ без второго списания. Для
                стрима ключ уходит, только если передан явно.
            timeout: таймаут чтения ответа, с. По умолчанию не меньше 600.
            **extras: max_tokens, temperature, tools, tool_choice,
                response_format, reasoning_effort и другие поля OpenAI API —
                передаются как есть.
        """
        body = _build_body({"model": model, "messages": messages}, extras)
        read_timeout = self._client._long_timeout(timeout)
        if stream:
            body["stream"] = True
            headers = {"Accept": "text/event-stream"}
            if idempotency_key:
                _validate_idempotency_key(idempotency_key)
                headers["Idempotency-Key"] = idempotency_key
            resp = self._client._send(
                "POST", "/v1/chat/completions", json=body, extra_headers=headers,
                timeout=read_timeout, retry_statuses=_SAFE_RETRY_STATUSES, stream=True,
            )
            return Stream(resp)
        headers = {"Idempotency-Key": _generate_idempotency_key(idempotency_key)}
        return cast(Union[ChatCompletion, Stream], self._client._request(
            "POST", "/v1/chat/completions", json=body, extra_headers=headers,
            timeout=read_timeout, retry_statuses=_CHAT_RETRY_STATUSES,
        ))


class Chat:
    def __init__(self, client: "NexusClient"):
        self.completions = ChatCompletions(client)


class V1Models(_Resource):
    def list(self) -> list[OpenAIModel]:
        """GET /v1/models — модели, доступные ключу, в формате OpenAI
        (`{"id", "object": "model", "created", "owned_by"}`). Нужен ключ."""
        data = self._client._request("GET", "/v1/models")
        return list((data or {}).get("data") or [])


class V1Images(_Resource):
    def generate(
        self,
        *,
        model: str,
        prompt: str,
        size: Optional[str] = None,
        quality: Optional[str] = None,
        seed: Optional[int] = None,
        idempotency_key: Optional[str] = None,
        timeout: Optional[float] = None,
        **extras: Any,
    ) -> ImagesResponse:
        """POST /v1/images/generations — синхронно: сервер ждёт готовую картинку
        (до 180 с, Seedream Pro до 540 с) и отдаёт
        `{"created": ..., "data": [{"url": ...}]}`.

        Args:
            size: "1024x1024", "1280x720" и т.п. — сервер переводит в aspect_ratio.
            quality: для моделей с выбором качества (например, Seedream Pro).
            **extras: native-параметры модели (image_url, negative_prompt, ...).
        """
        body = _build_body(
            {"model": model, "prompt": prompt},
            {"size": size, "quality": quality, "seed": seed, **extras},
        )
        headers = {"Idempotency-Key": _generate_idempotency_key(idempotency_key)}
        return cast(ImagesResponse, self._client._request(
            "POST", "/v1/images/generations", json=body, extra_headers=headers,
            timeout=self._client._long_timeout(timeout),
        ))


class V1Videos(_Resource):
    def create(
        self,
        *,
        model: str,
        prompt: str,
        seconds: Optional[Union[int, str]] = None,
        size: Optional[str] = None,
        input_reference: Optional[Union[str, list[str]]] = None,
        seed: Optional[int] = None,
        idempotency_key: Optional[str] = None,
        **extras: Any,
    ) -> Video:
        """POST /v1/videos — создаёт видео (Sora-style), сразу возвращает объект
        `{"id", "status": "queued", ...}`. Дождаться — client.v1.videos.wait(video).

        Args:
            seconds: длительность, с.
            size: "1920x1080", "1080x1920" и т.п.
            input_reference: URL кадра-референса (у Seedance 2.0 — список до 9 URL).
            **extras: resolution, image_urls, video_urls, audio_urls,
                webhook_url, negative_prompt и другие native-параметры.
        """
        body = _build_body(
            {"model": model, "prompt": prompt},
            {"seconds": seconds, "size": size, "input_reference": input_reference,
             "seed": seed, **extras},
        )
        headers = {"Idempotency-Key": _generate_idempotency_key(idempotency_key)}
        return cast(Video, self._client._request(
            "POST", "/v1/videos", json=body, extra_headers=headers,
            timeout=self._client._generate_timeout(),
        ))

    def retrieve(self, video: Union[str, Mapping[str, Any]]) -> Video:
        """GET /v1/videos/{id}. Статус: queued / in_progress / completed / failed;
        у готового видео ссылка в поле `url`."""
        return cast(Video, self._client._request("GET", _video_path(video)))

    def wait(
        self,
        video: Union[str, Mapping[str, Any]],
        *,
        timeout: float = DEFAULT_VIDEO_WAIT_TIMEOUT,
        poll_interval: float = DEFAULT_VIDEO_POLL_INTERVAL,
    ) -> Video:
        """Опрашивает видео до completed. Принимает id или объект из create().

        Временные ошибки опроса (сеть, 429, 5xx) пережидает до дедлайна.

        Raises:
            NexusTimeoutError: не дождались за `timeout` секунд (видео
                продолжает генерироваться; `.task_id` — его id).
            TaskFailedError: status='failed' (деньги возвращаются на ключ).
        """
        video_id = _video_id(video)
        deadline = time.monotonic() + timeout
        current: Video = {"id": video_id}
        while True:
            try:
                current = self.retrieve(video_id)
            except errors.NexusError as e:
                if not _is_transient(e) or time.monotonic() >= deadline:
                    e.task_id = video_id
                    raise
                logger.info("Polling video %s failed (%s), will retry", video_id, e)
            else:
                if current.get("status") in _VIDEO_DONE_STATUSES:
                    _raise_if_video_failed(current)
                    return current
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise _video_timeout_error(current, timeout)
            time.sleep(min(poll_interval, remaining))


class Webhooks(_Resource):
    """Подпись вебхуков: секрет и проверка входящих запросов.

    Если сервис, принимающий вебхуки, знает API-ключ, проще всего
    `client.webhooks.unwrap(body, headers)`: секрет вычисляется из ключа клиента.
    Сервису без ключа — `nexusapi.webhooks.unwrap(body, headers, secret=...)`
    с секретом из `client.webhooks.secret()`.
    """

    def secret(self) -> str:
        """GET /webhooks/secret — секрет `whsec_…` для задач этого ключа."""
        data = self._client._request("GET", "/webhooks/secret")
        return _require_secret(data)

    def unwrap(
        self,
        payload: _webhooks.Payload,
        headers: Mapping[str, Any],
        *,
        secret: Optional[str] = None,
        tolerance: float = _webhooks.DEFAULT_TOLERANCE,
    ) -> WebhookEvent:
        """Проверяет подпись вебхука и возвращает событие. `payload` — сырое тело
        запроса. Без `secret` берётся секрет ключа этого клиента.

        Raises:
            WebhookVerificationError: подпись не прошла проверку.
        """
        return _webhooks.unwrap(
            payload, headers, secret or _webhooks.secret_from_api_key(self._client._api_key),
            tolerance=tolerance,
        )

    def verify_signature(
        self,
        payload: _webhooks.Payload,
        headers: Mapping[str, Any],
        *,
        secret: Optional[str] = None,
        tolerance: float = _webhooks.DEFAULT_TOLERANCE,
    ) -> None:
        """Только проверка подписи (без разбора тела). См. unwrap()."""
        _webhooks.verify_signature(
            payload, headers, secret or _webhooks.secret_from_api_key(self._client._api_key),
            tolerance=tolerance,
        )


class V1:
    """OpenAI-совместимые эндпоинты /v1/*."""

    def __init__(self, client: "NexusClient", chat: Chat):
        self.chat = chat
        self.models = V1Models(client)
        self.images = V1Images(client)
        self.videos = V1Videos(client)


# ============================================================
# Async resources
# ============================================================


class _AsyncResource:
    def __init__(self, client: "AsyncNexusClient"):
        self._client = client


class AsyncVideos(_AsyncResource):
    async def create(
        self,
        *,
        model: str,
        prompt: str,
        duration: Optional[int] = None,
        aspect_ratio: Optional[str] = None,
        idempotency_key: Optional[str] = None,
        **extras: Any,
    ) -> AsyncTask:
        params = _build_generate_params(
            model=model, prompt=prompt, duration=duration,
            aspect_ratio=aspect_ratio, extras=extras,
        )
        headers = {"Idempotency-Key": _generate_idempotency_key(idempotency_key)}
        data = await self._client._request(
            "POST", "/generate", json={"params": params}, extra_headers=headers,
            timeout=self._client._generate_timeout(),
        )
        return AsyncTask(
            id=_require_task_id(data, headers["Idempotency-Key"]),
            status="pending",
            model=model,
            _client=self._client,
        )


class AsyncImages(_AsyncResource):
    async def generate(
        self,
        *,
        model: str,
        prompt: str,
        aspect_ratio: Optional[str] = None,
        wait: bool = True,
        timeout: float = DEFAULT_IMAGE_WAIT_TIMEOUT,
        poll_interval: float = DEFAULT_IMAGE_POLL_INTERVAL,
        idempotency_key: Optional[str] = None,
        **extras: Any,
    ) -> AsyncTask:
        params = _build_generate_params(
            model=model, prompt=prompt, aspect_ratio=aspect_ratio, extras=extras,
        )
        headers = {"Idempotency-Key": _generate_idempotency_key(idempotency_key)}
        data = await self._client._request(
            "POST", "/generate", json={"params": params}, extra_headers=headers,
            timeout=self._client._generate_timeout(),
        )
        task = AsyncTask(
            id=_require_task_id(data, headers["Idempotency-Key"]),
            status="pending", model=model, _client=self._client,
        )
        if wait:
            await task.wait(timeout=timeout, poll_interval=poll_interval)
        return task


class AsyncTasks(_AsyncResource):
    async def get(self, task_id: str) -> AsyncTask:
        data = await self._client._request("GET", _task_path(task_id))
        return AsyncTask(**_task_kwargs(data), _client=self._client)


class AsyncModels(_AsyncResource):
    async def list(self) -> list[ModelInfo]:
        data = await self._client._request("GET", "/public/models", auth=False)
        return list(data or [])


class AsyncChatCompletions(_AsyncResource):
    """Async-вариант ChatCompletions: тот же контракт, `await` и `async for`."""

    @overload
    async def create(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        stream: Literal[False] = False,
        idempotency_key: Optional[str] = None,
        timeout: Optional[float] = None,
        **extras: Any,
    ) -> ChatCompletion: ...

    @overload
    async def create(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        stream: Literal[True],
        idempotency_key: Optional[str] = None,
        timeout: Optional[float] = None,
        **extras: Any,
    ) -> AsyncStream: ...

    @overload
    async def create(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        stream: bool,
        idempotency_key: Optional[str] = None,
        timeout: Optional[float] = None,
        **extras: Any,
    ) -> Union[ChatCompletion, AsyncStream]: ...

    async def create(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        stream: bool = False,
        idempotency_key: Optional[str] = None,
        timeout: Optional[float] = None,
        **extras: Any,
    ) -> Union[ChatCompletion, AsyncStream]:
        body = _build_body({"model": model, "messages": messages}, extras)
        read_timeout = self._client._long_timeout(timeout)
        if stream:
            body["stream"] = True
            headers = {"Accept": "text/event-stream"}
            if idempotency_key:
                _validate_idempotency_key(idempotency_key)
                headers["Idempotency-Key"] = idempotency_key
            resp = await self._client._send(
                "POST", "/v1/chat/completions", json=body, extra_headers=headers,
                timeout=read_timeout, retry_statuses=_SAFE_RETRY_STATUSES, stream=True,
            )
            return AsyncStream(resp)
        headers = {"Idempotency-Key": _generate_idempotency_key(idempotency_key)}
        return cast(Union[ChatCompletion, AsyncStream], await self._client._request(
            "POST", "/v1/chat/completions", json=body, extra_headers=headers,
            timeout=read_timeout, retry_statuses=_CHAT_RETRY_STATUSES,
        ))


class AsyncChat:
    def __init__(self, client: "AsyncNexusClient"):
        self.completions = AsyncChatCompletions(client)


class AsyncV1Models(_AsyncResource):
    async def list(self) -> list[OpenAIModel]:
        data = await self._client._request("GET", "/v1/models")
        return list((data or {}).get("data") or [])


class AsyncV1Images(_AsyncResource):
    async def generate(
        self,
        *,
        model: str,
        prompt: str,
        size: Optional[str] = None,
        quality: Optional[str] = None,
        seed: Optional[int] = None,
        idempotency_key: Optional[str] = None,
        timeout: Optional[float] = None,
        **extras: Any,
    ) -> ImagesResponse:
        body = _build_body(
            {"model": model, "prompt": prompt},
            {"size": size, "quality": quality, "seed": seed, **extras},
        )
        headers = {"Idempotency-Key": _generate_idempotency_key(idempotency_key)}
        return cast(ImagesResponse, await self._client._request(
            "POST", "/v1/images/generations", json=body, extra_headers=headers,
            timeout=self._client._long_timeout(timeout),
        ))


class AsyncV1Videos(_AsyncResource):
    async def create(
        self,
        *,
        model: str,
        prompt: str,
        seconds: Optional[Union[int, str]] = None,
        size: Optional[str] = None,
        input_reference: Optional[Union[str, list[str]]] = None,
        seed: Optional[int] = None,
        idempotency_key: Optional[str] = None,
        **extras: Any,
    ) -> Video:
        body = _build_body(
            {"model": model, "prompt": prompt},
            {"seconds": seconds, "size": size, "input_reference": input_reference,
             "seed": seed, **extras},
        )
        headers = {"Idempotency-Key": _generate_idempotency_key(idempotency_key)}
        return cast(Video, await self._client._request(
            "POST", "/v1/videos", json=body, extra_headers=headers,
            timeout=self._client._generate_timeout(),
        ))

    async def retrieve(self, video: Union[str, Mapping[str, Any]]) -> Video:
        return cast(Video, await self._client._request("GET", _video_path(video)))

    async def wait(
        self,
        video: Union[str, Mapping[str, Any]],
        *,
        timeout: float = DEFAULT_VIDEO_WAIT_TIMEOUT,
        poll_interval: float = DEFAULT_VIDEO_POLL_INTERVAL,
    ) -> Video:
        video_id = _video_id(video)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        current: Video = {"id": video_id}
        while True:
            try:
                current = await self.retrieve(video_id)
            except errors.NexusError as e:
                if not _is_transient(e) or loop.time() >= deadline:
                    e.task_id = video_id
                    raise
                logger.info("Polling video %s failed (%s), will retry", video_id, e)
            else:
                if current.get("status") in _VIDEO_DONE_STATUSES:
                    _raise_if_video_failed(current)
                    return current
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise _video_timeout_error(current, timeout)
            await asyncio.sleep(min(poll_interval, remaining))


class AsyncWebhooks(_AsyncResource):
    """Async-вариант Webhooks: secret() — корутина, проверка подписи синхронная
    (это чистые вычисления без сети)."""

    async def secret(self) -> str:
        data = await self._client._request("GET", "/webhooks/secret")
        return _require_secret(data)

    def unwrap(
        self,
        payload: _webhooks.Payload,
        headers: Mapping[str, Any],
        *,
        secret: Optional[str] = None,
        tolerance: float = _webhooks.DEFAULT_TOLERANCE,
    ) -> WebhookEvent:
        return _webhooks.unwrap(
            payload, headers, secret or _webhooks.secret_from_api_key(self._client._api_key),
            tolerance=tolerance,
        )

    def verify_signature(
        self,
        payload: _webhooks.Payload,
        headers: Mapping[str, Any],
        *,
        secret: Optional[str] = None,
        tolerance: float = _webhooks.DEFAULT_TOLERANCE,
    ) -> None:
        _webhooks.verify_signature(
            payload, headers, secret or _webhooks.secret_from_api_key(self._client._api_key),
            tolerance=tolerance,
        )


class AsyncV1:
    def __init__(self, client: "AsyncNexusClient", chat: AsyncChat):
        self.chat = chat
        self.models = AsyncV1Models(client)
        self.images = AsyncV1Images(client)
        self.videos = AsyncV1Videos(client)


# ============================================================
# Clients
# ============================================================


class _ClientBase:
    """Общие настройки sync/async клиента."""

    _api_key: str
    _base_url: str
    _timeout: Union[float, httpx.Timeout]
    _max_retries: int
    _default_headers: dict[str, str]

    def _configure(
        self,
        api_key: Optional[str],
        base_url: Optional[str],
        timeout: Union[float, httpx.Timeout],
        max_retries: int,
        default_headers: Optional[Mapping[str, str]],
    ) -> None:
        api_key = api_key or os.environ.get(ENV_API_KEY)
        if not api_key:
            raise errors.AuthenticationError(
                f"api_key is required: pass api_key=... or set the {ENV_API_KEY} environment variable"
            )
        if max_retries < 0:
            raise ValueError("max_retries must be >= 0")
        self._api_key = api_key
        self._base_url = _normalize_base_url(
            base_url or os.environ.get(ENV_BASE_URL) or DEFAULT_BASE_URL
        )
        self._timeout = timeout
        self._max_retries = max_retries
        self._default_headers = dict(default_headers or {})

    def _merged_headers(
        self, default_headers: Union[Mapping[str, str], NotGiven],
    ) -> dict[str, str]:
        if isinstance(default_headers, NotGiven):
            return dict(self._default_headers)
        return {**self._default_headers, **default_headers}

    def _headers(self, auth: bool, extra_headers: Optional[Mapping[str, str]]) -> dict[str, str]:
        headers = {"User-Agent": _USER_AGENT, **self._default_headers}
        if auth:
            headers["Authorization"] = f"Bearer {self._api_key}"
        if extra_headers:
            headers.update(extra_headers)
        return headers

    def __repr__(self) -> str:
        # Без ключа: repr попадает в логи.
        return f"{type(self).__name__}(base_url={self._base_url!r})"


class NexusClient(_ClientBase):
    """Главный sync-клиент NexusAPI.

    Args:
        api_key: Bearer-токен (panel.nexusapi.dev → API ключи). Если не передан —
            из переменной окружения NEXUSAPI_API_KEY.
        base_url: URL API. По умолчанию NEXUSAPI_BASE_URL или https://nexusapi.dev.
        timeout: Таймаут HTTP-запросов, с (или httpx.Timeout). Дефолт 60. Для
            POST /generate чтение не меньше 120 с, для чата и
            /v1/images/generations — не меньше 600 с.
        max_retries: Сколько раз повторить при 5xx/429/сетевых ошибках.
            Дефолт 2 (то есть до 3 попыток всего). 0 — отключить повторы.
        default_headers: Заголовки для каждого запроса.
        http_client: Свой httpx.Client (прокси, TLS, лимиты соединений). SDK
            его не закрывает; таймаут и повторы берутся из настроек SDK.

    Разовые настройки — через копию: `client.with_options(timeout=5).tasks.get(id)`.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        *,
        base_url: Optional[str] = None,
        timeout: Union[float, httpx.Timeout] = DEFAULT_TIMEOUT,
        max_retries: int = DEFAULT_MAX_RETRIES,
        default_headers: Optional[Mapping[str, str]] = None,
        http_client: Optional[httpx.Client] = None,
        # Для тестов: позволяем подменить transport (httpx.MockTransport).
        transport: Optional[httpx.BaseTransport] = None,
    ):
        if http_client is not None and transport is not None:
            raise ValueError("pass either http_client or transport, not both")
        self._configure(api_key, base_url, timeout, max_retries, default_headers)
        self._owns_http = http_client is None
        self._http = http_client or httpx.Client(timeout=timeout, transport=transport)
        self.videos = Videos(self)
        self.images = Images(self)
        self.tasks = Tasks(self)
        self.models = Models(self)
        self.chat = Chat(self)
        self.v1 = V1(self, self.chat)
        self.webhooks = Webhooks(self)

    def with_options(
        self,
        *,
        timeout: Union[float, httpx.Timeout, NotGiven] = NOT_GIVEN,
        max_retries: Union[int, NotGiven] = NOT_GIVEN,
        default_headers: Union[Mapping[str, str], NotGiven] = NOT_GIVEN,
    ) -> "NexusClient":
        """Копия клиента с другими настройками на том же пуле соединений:
        `client.with_options(timeout=10, max_retries=0).videos.create(...)`.
        Заголовки дополняют текущие. Закрывать копию не нужно."""
        return NexusClient(
            self._api_key,
            base_url=self._base_url,
            timeout=self._timeout if isinstance(timeout, NotGiven) else timeout,
            max_retries=self._max_retries if isinstance(max_retries, NotGiven) else max_retries,
            default_headers=self._merged_headers(default_headers),
            http_client=self._http,
        )

    def _long_timeout(self, timeout: Optional[float]) -> httpx.Timeout:
        return _long_timeout(self._timeout, timeout)

    def _generate_timeout(self) -> httpx.Timeout:
        return _long_timeout(self._timeout, None, minimum=DEFAULT_GENERATE_TIMEOUT)

    def _send(
        self,
        method: str,
        path: str,
        *,
        json: Optional[dict[str, Any]] = None,
        params: Optional[dict[str, Any]] = None,
        auth: bool = True,
        extra_headers: Optional[Mapping[str, str]] = None,
        timeout: Any = None,
        retry_statuses: frozenset[int] = _RETRYABLE_STATUSES,
        stream: bool = False,
        retry_sent_errors: Optional[bool] = None,
    ) -> httpx.Response:
        """Отправляет запрос с повторами; возвращает успешный httpx.Response.

        stream=True: тело не читается (его читает Stream), а повтор при
        сетевой ошибке — только если запрос точно не ушёл (_NOT_SENT_ERRORS).
        retry_sent_errors=False — то же правило для обычного запроса (долгие
        /v1-запросы); по умолчанию False для стрима и True для остальных.
        """
        url = f"{self._base_url}{path}"
        headers = self._headers(auth, extra_headers)
        idem_key = headers.get("Idempotency-Key")
        if retry_sent_errors is None:
            retry_sent_errors = not stream

        attempt = 0
        conflict_attempt = 0
        conflict_deadline: Optional[float] = None
        while True:
            request = self._http.build_request(
                method, url, json=json, params=params, headers=headers,
                timeout=timeout if timeout is not None else self._timeout,
            )
            try:
                resp = self._http.send(request, stream=stream)
            except httpx.TransportError as e:
                if _retryable_transport_error(e, retry_sent_errors) and attempt < self._max_retries:
                    delay = _backoff_delay(attempt)
                    _log_retry(method, path, attempt, self._max_retries, delay, type(e).__name__)
                    time.sleep(delay)
                    attempt += 1
                    continue
                raise _network_error(e, attempt, idem_key, stream) from e

            if resp.status_code in retry_statuses and attempt < self._max_retries:
                delay = _retry_delay(resp, attempt)
                resp.close()
                _log_retry(method, path, attempt, self._max_retries, delay, f"HTTP {resp.status_code}")
                time.sleep(delay)
                attempt += 1
                continue

            # 409 после повтора самого SDK: первый запрос с этим ключом ещё
            # выполняется на сервере. Ждём его сохранённый ответ, а не сдаёмся.
            if resp.status_code == 409 and idem_key and attempt > 0 and not stream:
                now = time.monotonic()
                if conflict_deadline is None:
                    conflict_deadline = now + _CONFLICT_WAIT_BUDGET
                if now < conflict_deadline:
                    delay = min(_backoff_delay(conflict_attempt), 5.0, conflict_deadline - now)
                    resp.close()
                    _log_retry(method, path, attempt, self._max_retries, delay, "HTTP 409 (in progress)")
                    time.sleep(delay)
                    conflict_attempt += 1
                    continue

            if not resp.is_success:
                if stream:
                    try:
                        resp.read()
                    finally:
                        resp.close()
                raise _status_error(resp, idem_key)
            logger.debug(
                "%s %s -> %d (request_id=%s)", method, path, resp.status_code,
                resp.headers.get("x-request-id"),
            )
            return resp

    def _request(
        self,
        method: str,
        path: str,
        *,
        json: Optional[dict[str, Any]] = None,
        params: Optional[dict[str, Any]] = None,
        auth: bool = True,
        extra_headers: Optional[Mapping[str, str]] = None,
        timeout: Any = None,
        retry_statuses: frozenset[int] = _RETRYABLE_STATUSES,
        retry_sent_errors: bool = True,
    ) -> Any:
        resp = self._send(
            method, path, json=json, params=params, auth=auth,
            extra_headers=extra_headers, timeout=timeout,
            retry_statuses=retry_statuses, retry_sent_errors=retry_sent_errors,
        )
        return _decode_json(resp, (extra_headers or {}).get("Idempotency-Key"))

    def close(self) -> None:
        """Закрывает пул соединений (если его создал SDK, а не передали снаружи)."""
        if self._owns_http:
            self._http.close()

    def __enter__(self) -> "NexusClient":
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()


class AsyncNexusClient(_ClientBase):
    """Главный async-клиент NexusAPI.

    Использование:

        async with AsyncNexusClient() as client:  # ключ из NEXUSAPI_API_KEY
            task = await client.videos.create(model="veo-3-fast", prompt="...")
            await task.wait()
            print(task.video_url)

    Все методы — корутины. Параметры конструктора и публичный API идентичны
    sync-варианту NexusClient; `http_client` — httpx.AsyncClient.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        *,
        base_url: Optional[str] = None,
        timeout: Union[float, httpx.Timeout] = DEFAULT_TIMEOUT,
        max_retries: int = DEFAULT_MAX_RETRIES,
        default_headers: Optional[Mapping[str, str]] = None,
        http_client: Optional[httpx.AsyncClient] = None,
        transport: Optional[httpx.AsyncBaseTransport] = None,
    ):
        if http_client is not None and transport is not None:
            raise ValueError("pass either http_client or transport, not both")
        self._configure(api_key, base_url, timeout, max_retries, default_headers)
        self._owns_http = http_client is None
        self._http = http_client or httpx.AsyncClient(timeout=timeout, transport=transport)
        self.videos = AsyncVideos(self)
        self.images = AsyncImages(self)
        self.tasks = AsyncTasks(self)
        self.models = AsyncModels(self)
        self.chat = AsyncChat(self)
        self.v1 = AsyncV1(self, self.chat)
        self.webhooks = AsyncWebhooks(self)

    def with_options(
        self,
        *,
        timeout: Union[float, httpx.Timeout, NotGiven] = NOT_GIVEN,
        max_retries: Union[int, NotGiven] = NOT_GIVEN,
        default_headers: Union[Mapping[str, str], NotGiven] = NOT_GIVEN,
    ) -> "AsyncNexusClient":
        """Копия клиента с другими настройками на том же пуле соединений."""
        return AsyncNexusClient(
            self._api_key,
            base_url=self._base_url,
            timeout=self._timeout if isinstance(timeout, NotGiven) else timeout,
            max_retries=self._max_retries if isinstance(max_retries, NotGiven) else max_retries,
            default_headers=self._merged_headers(default_headers),
            http_client=self._http,
        )

    def _long_timeout(self, timeout: Optional[float]) -> httpx.Timeout:
        return _long_timeout(self._timeout, timeout)

    def _generate_timeout(self) -> httpx.Timeout:
        return _long_timeout(self._timeout, None, minimum=DEFAULT_GENERATE_TIMEOUT)

    async def _send(
        self,
        method: str,
        path: str,
        *,
        json: Optional[dict[str, Any]] = None,
        params: Optional[dict[str, Any]] = None,
        auth: bool = True,
        extra_headers: Optional[Mapping[str, str]] = None,
        timeout: Any = None,
        retry_statuses: frozenset[int] = _RETRYABLE_STATUSES,
        stream: bool = False,
        retry_sent_errors: Optional[bool] = None,
    ) -> httpx.Response:
        url = f"{self._base_url}{path}"
        headers = self._headers(auth, extra_headers)
        idem_key = headers.get("Idempotency-Key")
        if retry_sent_errors is None:
            retry_sent_errors = not stream

        loop = asyncio.get_running_loop()
        attempt = 0
        conflict_attempt = 0
        conflict_deadline: Optional[float] = None
        while True:
            request = self._http.build_request(
                method, url, json=json, params=params, headers=headers,
                timeout=timeout if timeout is not None else self._timeout,
            )
            try:
                resp = await self._http.send(request, stream=stream)
            except httpx.TransportError as e:
                if _retryable_transport_error(e, retry_sent_errors) and attempt < self._max_retries:
                    delay = _backoff_delay(attempt)
                    _log_retry(method, path, attempt, self._max_retries, delay, type(e).__name__)
                    await asyncio.sleep(delay)
                    attempt += 1
                    continue
                raise _network_error(e, attempt, idem_key, stream) from e

            if resp.status_code in retry_statuses and attempt < self._max_retries:
                delay = _retry_delay(resp, attempt)
                await resp.aclose()
                _log_retry(method, path, attempt, self._max_retries, delay, f"HTTP {resp.status_code}")
                await asyncio.sleep(delay)
                attempt += 1
                continue

            # 409 после повтора самого SDK: первый запрос с этим ключом ещё
            # выполняется на сервере. Ждём его сохранённый ответ, а не сдаёмся.
            if resp.status_code == 409 and idem_key and attempt > 0 and not stream:
                now = loop.time()
                if conflict_deadline is None:
                    conflict_deadline = now + _CONFLICT_WAIT_BUDGET
                if now < conflict_deadline:
                    delay = min(_backoff_delay(conflict_attempt), 5.0, conflict_deadline - now)
                    await resp.aclose()
                    _log_retry(method, path, attempt, self._max_retries, delay, "HTTP 409 (in progress)")
                    await asyncio.sleep(delay)
                    conflict_attempt += 1
                    continue

            if not resp.is_success:
                if stream:
                    try:
                        await resp.aread()
                    finally:
                        await resp.aclose()
                raise _status_error(resp, idem_key)
            logger.debug(
                "%s %s -> %d (request_id=%s)", method, path, resp.status_code,
                resp.headers.get("x-request-id"),
            )
            return resp

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: Optional[dict[str, Any]] = None,
        params: Optional[dict[str, Any]] = None,
        auth: bool = True,
        extra_headers: Optional[Mapping[str, str]] = None,
        timeout: Any = None,
        retry_statuses: frozenset[int] = _RETRYABLE_STATUSES,
        retry_sent_errors: bool = True,
    ) -> Any:
        resp = await self._send(
            method, path, json=json, params=params, auth=auth,
            extra_headers=extra_headers, timeout=timeout,
            retry_statuses=retry_statuses, retry_sent_errors=retry_sent_errors,
        )
        return _decode_json(resp, (extra_headers or {}).get("Idempotency-Key"))

    async def close(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    async def __aenter__(self) -> "AsyncNexusClient":
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.close()


def _retryable_transport_error(e: Exception, retry_sent_errors: bool) -> bool:
    if not isinstance(e, (httpx.NetworkError, httpx.TimeoutException)):
        return False  # протокол/прокси/неверный URL — повтор не поможет
    return retry_sent_errors or isinstance(e, _NOT_SENT_ERRORS)


def _log_retry(
    method: str, path: str, attempt: int, max_retries: int, delay: float, reason: str,
) -> None:
    logger.info(
        "%s on %s %s (attempt %d of %d), retrying in %.2fs",
        reason, method, path, attempt + 1, max_retries + 1, delay,
    )


def _network_error(
    e: Exception, attempt: int, idem_key: Optional[str], stream: bool,
) -> errors.NetworkError:
    reason = f"{type(e).__name__}: {e}" if str(e) else type(e).__name__
    if isinstance(e, _NOT_SENT_ERRORS):
        message = f"Could not connect after {attempt + 1} attempts: {reason}"
    else:
        message = f"Network error after {attempt + 1} attempts: {reason}"
        if stream:
            message += (
                ". The request may have reached the server; a streamed response "
                "cannot be replayed"
            )
        elif idem_key:
            message += (
                f". The request may still be running on the server: repeat it with "
                f"idempotency_key={idem_key!r}, not a new key — the server returns the "
                f"result of the first request instead of starting a new one"
            )
    cls = errors.APITimeoutError if isinstance(e, httpx.TimeoutException) else errors.NetworkError
    return cls(message, idempotency_key=idem_key)


def _status_error(resp: httpx.Response, idem_key: Optional[str]) -> errors.APIStatusError:
    message, cls, body = _parse_error(resp)
    err = cls(
        message, status_code=resp.status_code, response_body=body,
        idempotency_key=idem_key, headers=dict(resp.headers),
        request_id=resp.headers.get("x-request-id"),
    )
    if isinstance(body, dict) and isinstance(body.get("error"), dict):
        detail = body["error"]
        for name in ("code", "type", "param"):
            value = detail.get(name)
            if isinstance(value, str):
                setattr(err, name, value)
        # 504 у /v1/images: задача идёт и будет оплачена — её id.
        task_id = detail.get("task_id")
        if isinstance(task_id, str) and task_id:
            err.task_id = task_id
    return err


def _require_secret(data: Any) -> str:
    secret = data.get("secret") if isinstance(data, dict) else None
    if not isinstance(secret, str) or not secret:
        raise errors.APIResponseValidationError(
            "Server response has no webhook secret", response_body=data,
        )
    return secret


def _decode_json(resp: httpx.Response, idem_key: Optional[str]) -> Any:
    if resp.status_code == 204 or not resp.content:
        return None
    try:
        return resp.json()
    except ValueError as e:
        raise errors.APIResponseValidationError(
            f"Invalid JSON in HTTP {resp.status_code} response",
            status_code=resp.status_code, response_body=_truncate(resp.text, 2000),
            idempotency_key=idem_key, headers=dict(resp.headers),
            request_id=resp.headers.get("x-request-id"),
        ) from e


def _normalize_base_url(base_url: str) -> str:
    url = base_url.rstrip("/")
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise ValueError(f"base_url must be an http(s) URL, got {base_url!r}")
    if url.endswith("/v1"):
        url = url[: -len("/v1")]
        warnings.warn(
            f"base_url should not end with /v1 (the SDK adds it where needed); "
            f"using {url!r}",
            UserWarning,
            stacklevel=3,
        )
    return url


def _long_timeout(
    client_timeout: Any,
    timeout: Optional[float],
    minimum: float = DEFAULT_LONG_REQUEST_TIMEOUT,
) -> httpx.Timeout:
    """Таймаут для долгих запросов: чтение — `timeout` или не меньше `minimum`,
    подключение — обычный таймаут клиента."""
    if isinstance(client_timeout, httpx.Timeout):
        connect, base = client_timeout.connect, client_timeout.read
    else:
        connect = base = client_timeout
    if timeout is not None:
        read = timeout
    elif base is None:
        read = None  # клиент без таймаута — так и оставляем
    else:
        read = max(base, minimum)
    if connect is not None and read is not None:
        connect = min(connect, read)
    return httpx.Timeout(read, connect=connect)
