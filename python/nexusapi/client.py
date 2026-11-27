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
import json as _json
import logging
import random
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import (
    Any,
    AsyncIterator,
    Iterable,
    Iterator,
    Literal,
    Optional,
    Type,
    Union,
    overload,
)
from urllib.parse import quote

import httpx

from . import errors

DEFAULT_BASE_URL = "https://nexusapi.dev"
DEFAULT_TIMEOUT = 60.0
DEFAULT_MAX_RETRIES = 2

# Default poll intervals for Task.wait — короче для image-моделей (~5-15s),
# длиннее для video (минуты).
DEFAULT_VIDEO_POLL_INTERVAL = 3.0
DEFAULT_IMAGE_POLL_INTERVAL = 1.5
DEFAULT_VIDEO_WAIT_TIMEOUT = 600.0  # 10 min
DEFAULT_IMAGE_WAIT_TIMEOUT = 180.0  # 3 min

# Синхронные /v1-запросы (чат, /v1/images/generations) сервер держит долго:
# картинки до 180 с (Seedream Pro до 540 с), ответ LLM — минуты. Таймаут
# чтения для них — не меньше 600 с (лимит прокси API), подключение — по
# обычному таймауту клиента.
DEFAULT_LONG_REQUEST_TIMEOUT = 600.0

logger = logging.getLogger("nexusapi")

# Статусы которые retry'им. 429 — rate limit (даём время отпустить),
# 5xx — серверная нестабильность которая может пройти сама.
_RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})

# Для долгих синхронных /v1-запросов 500/502/504 НЕ повторяем: сервер мог уже
# создать платную задачу (например, 504 у /v1/images/generations значит
# «задача ещё идёт»), а 5xx не кэшируется по Idempotency-Key — повтор создал
# бы вторую задачу и второе списание. 429 и 503 приходят до создания задачи.
_SAFE_RETRY_STATUSES = frozenset({429, 503})

# Ошибки, при которых запрос гарантированно не ушёл на сервер. Только их
# повторяем для SSE-стрима: стрим нельзя переотправить с середины.
_NOT_SENT_ERRORS = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)

# Http-методы которые ВСЕГДА можно безопасно retry'ить (идемпотентны).
# POST в принципе не safe, но мы автоматически проставляем Idempotency-Key
# на /generate — так что и POST тоже безопасен.
_IDEMPOTENT_METHODS = frozenset({"GET", "DELETE", "PUT", "HEAD", "OPTIONS"})


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
_USER_AGENT = f"nexusapi-python/{VERSION}"


def _backoff_delay(attempt: int) -> float:
    """Exponential backoff с jitter. attempt начинается с 0.

    0 → 0.5..0.8s, 1 → 1.0..1.3s, 2 → 2.0..2.3s, 3+ → 4.0..4.3s.
    """
    base = min(4.0, 0.5 * (2 ** attempt))
    return base + random.uniform(0, 0.3)


def _parse_error(resp: httpx.Response) -> tuple[str, Type[errors.NexusError], Any]:
    """Парсит response → (message, exception_class, response_body).

    Используется в обоих sync/async вариантах _raise_for_status — extract
    в отдельный helper потому что respond_status маппинг и message-extraction
    одинаковы.
    """
    body: Any = None
    message = f"HTTP {resp.status_code}"
    try:
        body = resp.json()
    except Exception:
        body = None

    if isinstance(body, dict):
        if isinstance(body.get("error"), dict) and body["error"].get("message"):
            # OpenAI-shape (/v1/*)
            message = body["error"]["message"]
        elif isinstance(body.get("detail"), str):
            # FastAPI string detail
            message = body["detail"]
        elif isinstance(body.get("detail"), list) and body["detail"]:
            # Pydantic-validation массив с loc/msg
            first = body["detail"][0]
            loc = ".".join(str(x) for x in (first.get("loc") or [])[1:]) or "request"
            msg = first.get("msg") or "invalid"
            message = f"{loc}: {msg}"
    else:
        text = resp.text
        if text:
            message = text

    cls_map: dict[int, Type[errors.NexusError]] = {
        400: errors.ValidationError,
        401: errors.AuthenticationError,
        402: errors.BillingError,
        403: errors.PermissionError,
        404: errors.NotFoundError,
        409: errors.ConflictError,
        422: errors.ValidationError,
        429: errors.RateLimitError,
    }
    cls = cls_map.get(resp.status_code, errors.NexusError)
    return message, cls, body


def _retry_delay(resp: httpx.Response, attempt: int) -> float:
    """Задержка перед повтором: Retry-After (секунды), если есть, иначе backoff."""
    retry_after = resp.headers.get("retry-after")
    try:
        return float(retry_after) if retry_after else _backoff_delay(attempt)
    except (TypeError, ValueError):
        return _backoff_delay(attempt)


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

    def _iter_events(self) -> Iterator[dict]:
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

    def __iter__(self) -> Iterator[dict]:
        return self._iterator

    def __next__(self) -> dict:
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

    async def _iter_events(self) -> AsyncIterator[dict]:
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

    def __aiter__(self) -> AsyncIterator[dict]:
        return self._iterator

    async def __anext__(self) -> dict:
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
    result: Optional[dict] = None
    error: Optional[str] = None
    created_at: Optional[str] = None

    @property
    def is_done(self) -> bool:
        return self.status in ("completed", "failed")

    @property
    def video_url(self) -> Optional[str]:
        return (self.result or {}).get("video_url")

    @property
    def image_url(self) -> Optional[str]:
        """Берёт image_urls[0] (для моделей возвращающих список) или image_url."""
        r = self.result or {}
        urls = r.get("image_urls")
        if isinstance(urls, list) and urls:
            return urls[0]
        return r.get("image_url")

    @property
    def image_urls(self) -> list:
        return list((self.result or {}).get("image_urls") or [])


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

        Raises:
            TimeoutError: если задача не завершилась за `timeout` секунд.
            TaskFailedError: если задача завершилась со status='failed'.
        """
        deadline = time.monotonic() + timeout
        while not self.is_done:
            if time.monotonic() > deadline:
                raise errors.TimeoutError(
                    f"Task {self.id} did not complete within {timeout}s",
                    response_body={"task_id": self.id, "last_status": self.status},
                )
            time.sleep(poll_interval)
            self.refresh()
        if self.status == "failed":
            raise errors.TaskFailedError(
                f"Task {self.id} failed: {self.error or 'unknown error'}",
                response_body={"task_id": self.id, "error": self.error},
            )
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
        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout
        while not self.is_done:
            if loop.time() > deadline:
                raise errors.TimeoutError(
                    f"Task {self.id} did not complete within {timeout}s",
                    response_body={"task_id": self.id, "last_status": self.status},
                )
            await asyncio.sleep(poll_interval)
            await self.refresh()
        if self.status == "failed":
            raise errors.TaskFailedError(
                f"Task {self.id} failed: {self.error or 'unknown error'}",
                response_body={"task_id": self.id, "error": self.error},
            )
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
        return user_key
    return f"sdk-{uuid.uuid4()}"


def _build_body(required: dict[str, Any], optional: dict[str, Any]) -> dict[str, Any]:
    """Тело /v1-запроса: обязательные поля + опциональные без None."""
    body = dict(required)
    for k, v in optional.items():
        if v is not None:
            body[k] = v
    return body


def _video_path(video: Union[str, dict]) -> str:
    video_id = video.get("id") if isinstance(video, dict) else video
    if not video_id:
        raise ValueError("video id is required")
    return f"/v1/videos/{quote(str(video_id), safe='')}"


def _raise_if_video_failed(video: dict) -> None:
    if video.get("status") != "failed":
        return
    err = video.get("error") or {}
    message = err.get("message") if isinstance(err, dict) else None
    raise errors.TaskFailedError(
        f"Video {video.get('id')} failed: {message or 'unknown error'}",
        response_body={"task_id": video.get("id"), "error": message, "video": video},
    )


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
        )
        return Task(
            id=data["task_id"],
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
        )
        task = Task(
            id=data["task_id"], status="pending", model=model, _client=self._client,
        )
        if wait:
            task.wait(timeout=timeout, poll_interval=poll_interval)
        return task


class Tasks(_Resource):
    def get(self, task_id: str) -> Task:
        data = self._client._request("GET", f"/tasks/{task_id}")
        return Task(
            id=data["task_id"],
            status=data["status"],
            model=None,
            result=data.get("result"),
            error=data.get("error"),
            created_at=data.get("created_at"),
            _client=self._client,
        )


class Models(_Resource):
    def list(self) -> list[dict]:
        """Возвращает каталог моделей с ценами. Использует публичный
        /public/models (без ключа). Список в OpenAI-формате — client.v1.models.list()."""
        return self._client._request("GET", "/public/models", auth=False)


class ChatCompletions(_Resource):
    """POST /v1/chat/completions — текстовые модели (Claude, GPT) в формате OpenAI."""

    @overload
    def create(
        self,
        *,
        model: str,
        messages: list[dict],
        stream: Literal[False] = False,
        idempotency_key: Optional[str] = None,
        timeout: Optional[float] = None,
        **extras: Any,
    ) -> dict: ...

    @overload
    def create(
        self,
        *,
        model: str,
        messages: list[dict],
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
        messages: list[dict],
        stream: bool,
        idempotency_key: Optional[str] = None,
        timeout: Optional[float] = None,
        **extras: Any,
    ) -> Union[dict, Stream]: ...

    def create(
        self,
        *,
        model: str,
        messages: list[dict],
        stream: bool = False,
        idempotency_key: Optional[str] = None,
        timeout: Optional[float] = None,
        **extras: Any,
    ) -> Union[dict, Stream]:
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
                headers["Idempotency-Key"] = idempotency_key
            resp = self._client._send(
                "POST", "/v1/chat/completions", json=body, extra_headers=headers,
                timeout=read_timeout, retry_statuses=_SAFE_RETRY_STATUSES, stream=True,
            )
            return Stream(resp)
        headers = {"Idempotency-Key": _generate_idempotency_key(idempotency_key)}
        return self._client._request(
            "POST", "/v1/chat/completions", json=body, extra_headers=headers,
            timeout=read_timeout, retry_statuses=_SAFE_RETRY_STATUSES,
            retry_sent_errors=False,
        )


class Chat:
    def __init__(self, client: "NexusClient"):
        self.completions = ChatCompletions(client)


class V1Models(_Resource):
    def list(self) -> list[dict]:
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
    ) -> dict:
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
        return self._client._request(
            "POST", "/v1/images/generations", json=body, extra_headers=headers,
            timeout=self._client._long_timeout(timeout),
            retry_statuses=_SAFE_RETRY_STATUSES,
            retry_sent_errors=False,
        )


class V1Videos(_Resource):
    def create(
        self,
        *,
        model: str,
        prompt: str,
        seconds: Optional[Union[int, str]] = None,
        size: Optional[str] = None,
        input_reference: Optional[Union[str, list]] = None,
        seed: Optional[int] = None,
        idempotency_key: Optional[str] = None,
        **extras: Any,
    ) -> dict:
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
        return self._client._request(
            "POST", "/v1/videos", json=body, extra_headers=headers,
        )

    def retrieve(self, video: Union[str, dict]) -> dict:
        """GET /v1/videos/{id}. Статус: queued / in_progress / completed / failed;
        у готового видео ссылка в поле `url`."""
        return self._client._request("GET", _video_path(video))

    def wait(
        self,
        video: Union[str, dict],
        *,
        timeout: float = DEFAULT_VIDEO_WAIT_TIMEOUT,
        poll_interval: float = DEFAULT_VIDEO_POLL_INTERVAL,
    ) -> dict:
        """Опрашивает видео до completed. Принимает id или объект из create().

        Raises:
            NexusTimeoutError: не дождались за `timeout` секунд.
            TaskFailedError: status='failed' (деньги возвращаются на ключ).
        """
        deadline = time.monotonic() + timeout
        while True:
            current = self.retrieve(video)
            if current.get("status") in _VIDEO_DONE_STATUSES:
                _raise_if_video_failed(current)
                return current
            if time.monotonic() > deadline:
                raise errors.TimeoutError(
                    f"Video {current.get('id')} did not complete within {timeout}s",
                    response_body={"task_id": current.get("id"),
                                   "last_status": current.get("status")},
                )
            time.sleep(poll_interval)


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
        )
        return AsyncTask(
            id=data["task_id"],
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
        )
        task = AsyncTask(
            id=data["task_id"], status="pending", model=model, _client=self._client,
        )
        if wait:
            await task.wait(timeout=timeout, poll_interval=poll_interval)
        return task


class AsyncTasks(_AsyncResource):
    async def get(self, task_id: str) -> AsyncTask:
        data = await self._client._request("GET", f"/tasks/{task_id}")
        return AsyncTask(
            id=data["task_id"],
            status=data["status"],
            model=None,
            result=data.get("result"),
            error=data.get("error"),
            created_at=data.get("created_at"),
            _client=self._client,
        )


class AsyncModels(_AsyncResource):
    async def list(self) -> list[dict]:
        return await self._client._request("GET", "/public/models", auth=False)


class AsyncChatCompletions(_AsyncResource):
    """Async-вариант ChatCompletions: тот же контракт, `await` и `async for`."""

    @overload
    async def create(
        self,
        *,
        model: str,
        messages: list[dict],
        stream: Literal[False] = False,
        idempotency_key: Optional[str] = None,
        timeout: Optional[float] = None,
        **extras: Any,
    ) -> dict: ...

    @overload
    async def create(
        self,
        *,
        model: str,
        messages: list[dict],
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
        messages: list[dict],
        stream: bool,
        idempotency_key: Optional[str] = None,
        timeout: Optional[float] = None,
        **extras: Any,
    ) -> Union[dict, AsyncStream]: ...

    async def create(
        self,
        *,
        model: str,
        messages: list[dict],
        stream: bool = False,
        idempotency_key: Optional[str] = None,
        timeout: Optional[float] = None,
        **extras: Any,
    ) -> Union[dict, AsyncStream]:
        body = _build_body({"model": model, "messages": messages}, extras)
        read_timeout = self._client._long_timeout(timeout)
        if stream:
            body["stream"] = True
            headers = {"Accept": "text/event-stream"}
            if idempotency_key:
                headers["Idempotency-Key"] = idempotency_key
            resp = await self._client._send(
                "POST", "/v1/chat/completions", json=body, extra_headers=headers,
                timeout=read_timeout, retry_statuses=_SAFE_RETRY_STATUSES, stream=True,
            )
            return AsyncStream(resp)
        headers = {"Idempotency-Key": _generate_idempotency_key(idempotency_key)}
        return await self._client._request(
            "POST", "/v1/chat/completions", json=body, extra_headers=headers,
            timeout=read_timeout, retry_statuses=_SAFE_RETRY_STATUSES,
            retry_sent_errors=False,
        )


class AsyncChat:
    def __init__(self, client: "AsyncNexusClient"):
        self.completions = AsyncChatCompletions(client)


class AsyncV1Models(_AsyncResource):
    async def list(self) -> list[dict]:
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
    ) -> dict:
        body = _build_body(
            {"model": model, "prompt": prompt},
            {"size": size, "quality": quality, "seed": seed, **extras},
        )
        headers = {"Idempotency-Key": _generate_idempotency_key(idempotency_key)}
        return await self._client._request(
            "POST", "/v1/images/generations", json=body, extra_headers=headers,
            timeout=self._client._long_timeout(timeout),
            retry_statuses=_SAFE_RETRY_STATUSES,
            retry_sent_errors=False,
        )


class AsyncV1Videos(_AsyncResource):
    async def create(
        self,
        *,
        model: str,
        prompt: str,
        seconds: Optional[Union[int, str]] = None,
        size: Optional[str] = None,
        input_reference: Optional[Union[str, list]] = None,
        seed: Optional[int] = None,
        idempotency_key: Optional[str] = None,
        **extras: Any,
    ) -> dict:
        body = _build_body(
            {"model": model, "prompt": prompt},
            {"seconds": seconds, "size": size, "input_reference": input_reference,
             "seed": seed, **extras},
        )
        headers = {"Idempotency-Key": _generate_idempotency_key(idempotency_key)}
        return await self._client._request(
            "POST", "/v1/videos", json=body, extra_headers=headers,
        )

    async def retrieve(self, video: Union[str, dict]) -> dict:
        return await self._client._request("GET", _video_path(video))

    async def wait(
        self,
        video: Union[str, dict],
        *,
        timeout: float = DEFAULT_VIDEO_WAIT_TIMEOUT,
        poll_interval: float = DEFAULT_VIDEO_POLL_INTERVAL,
    ) -> dict:
        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout
        while True:
            current = await self.retrieve(video)
            if current.get("status") in _VIDEO_DONE_STATUSES:
                _raise_if_video_failed(current)
                return current
            if loop.time() > deadline:
                raise errors.TimeoutError(
                    f"Video {current.get('id')} did not complete within {timeout}s",
                    response_body={"task_id": current.get("id"),
                                   "last_status": current.get("status")},
                )
            await asyncio.sleep(poll_interval)


class AsyncV1:
    def __init__(self, client: "AsyncNexusClient", chat: AsyncChat):
        self.chat = chat
        self.models = AsyncV1Models(client)
        self.images = AsyncV1Images(client)
        self.videos = AsyncV1Videos(client)


# ============================================================
# Clients
# ============================================================


class NexusClient:
    """Главный sync-клиент NexusAPI.

    Args:
        api_key: Bearer-токен. Создаётся в panel.nexusapi.dev → API ключи.
        base_url: URL API. Дефолт https://nexusapi.dev.
        timeout: Таймаут HTTP-запросов в секундах. Дефолт 60. Для чата и
            /v1/images/generations таймаут чтения не меньше 600 с.
        max_retries: Сколько раз повторить при 5xx/429/network-ошибках.
            Дефолт 2 (то есть до 3 попыток всего). 0 — отключить retries.
    """

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = DEFAULT_TIMEOUT,
        max_retries: int = DEFAULT_MAX_RETRIES,
        # Для тестов: позволяем подменить transport (httpx.MockTransport).
        transport: Optional[httpx.BaseTransport] = None,
    ):
        if not api_key:
            raise errors.AuthenticationError("api_key is required")
        if max_retries < 0:
            raise ValueError("max_retries must be >= 0")
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._max_retries = max_retries
        self._http = httpx.Client(
            timeout=timeout,
            headers={"User-Agent": _USER_AGENT},
            transport=transport,
        )
        self.videos = Videos(self)
        self.images = Images(self)
        self.tasks = Tasks(self)
        self.models = Models(self)
        self.chat = Chat(self)
        self.v1 = V1(self, self.chat)

    def _long_timeout(self, timeout: Optional[float]) -> httpx.Timeout:
        return _long_timeout(self._timeout, timeout)

    def _send(
        self,
        method: str,
        path: str,
        *,
        json: Optional[dict] = None,
        params: Optional[dict] = None,
        auth: bool = True,
        extra_headers: Optional[dict] = None,
        timeout: Any = None,
        retry_statuses: frozenset = _RETRYABLE_STATUSES,
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
        headers = _request_headers(self._api_key, auth, extra_headers)
        idem_key = headers.get("Idempotency-Key")
        if retry_sent_errors is None:
            retry_sent_errors = not stream

        attempt = 0
        while True:
            request = self._http.build_request(
                method, url, json=json, params=params, headers=headers,
                timeout=timeout if timeout is not None else httpx.USE_CLIENT_DEFAULT,
            )
            try:
                resp = self._http.send(request, stream=stream)
            except httpx.TransportError as e:
                retryable = isinstance(e, (httpx.NetworkError, httpx.TimeoutException)) and (
                    retry_sent_errors or isinstance(e, _NOT_SENT_ERRORS)
                )
                if retryable and attempt < self._max_retries:
                    delay = _backoff_delay(attempt)
                    logger.debug(
                        "Network error %s on %s %s (attempt %d/%d), retrying in %.2fs",
                        e.__class__.__name__, method, path, attempt + 1,
                        self._max_retries + 1, delay,
                    )
                    time.sleep(delay)
                    attempt += 1
                    continue
                raise _network_error(e, attempt, idem_key) from e

            if resp.status_code in retry_statuses and attempt < self._max_retries:
                delay = _retry_delay(resp, attempt)
                resp.close()
                logger.debug(
                    "HTTP %d on %s %s (attempt %d/%d), retrying in %.2fs",
                    resp.status_code, method, path, attempt + 1,
                    self._max_retries + 1, delay,
                )
                time.sleep(delay)
                attempt += 1
                continue

            if not resp.is_success:
                if stream:
                    try:
                        resp.read()
                    finally:
                        resp.close()
                message, cls, body = _parse_error(resp)
                raise cls(
                    message, status_code=resp.status_code, response_body=body,
                    idempotency_key=idem_key,
                )
            return resp

    def _request(
        self,
        method: str,
        path: str,
        *,
        json: Optional[dict] = None,
        params: Optional[dict] = None,
        auth: bool = True,
        extra_headers: Optional[dict] = None,
        timeout: Any = None,
        retry_statuses: frozenset = _RETRYABLE_STATUSES,
        retry_sent_errors: bool = True,
    ) -> Any:
        resp = self._send(
            method, path, json=json, params=params, auth=auth,
            extra_headers=extra_headers, timeout=timeout,
            retry_statuses=retry_statuses, retry_sent_errors=retry_sent_errors,
        )
        if resp.status_code == 204 or not resp.content:
            return None
        return resp.json()

    def close(self) -> None:
        self._http.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class AsyncNexusClient:
    """Главный async-клиент NexusAPI.

    Использование:

        async with AsyncNexusClient(api_key="...") as client:
            task = await client.videos.create(model="veo-3-fast", prompt="...")
            await task.wait()
            print(task.video_url)

    Все методы — корутины. Параметры конструктора и публичный API идентичны
    sync-варианту NexusClient.
    """

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = DEFAULT_TIMEOUT,
        max_retries: int = DEFAULT_MAX_RETRIES,
        transport: Optional[httpx.AsyncBaseTransport] = None,
    ):
        if not api_key:
            raise errors.AuthenticationError("api_key is required")
        if max_retries < 0:
            raise ValueError("max_retries must be >= 0")
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._max_retries = max_retries
        self._http = httpx.AsyncClient(
            timeout=timeout,
            headers={"User-Agent": _USER_AGENT},
            transport=transport,
        )
        self.videos = AsyncVideos(self)
        self.images = AsyncImages(self)
        self.tasks = AsyncTasks(self)
        self.models = AsyncModels(self)
        self.chat = AsyncChat(self)
        self.v1 = AsyncV1(self, self.chat)

    def _long_timeout(self, timeout: Optional[float]) -> httpx.Timeout:
        return _long_timeout(self._timeout, timeout)

    async def _send(
        self,
        method: str,
        path: str,
        *,
        json: Optional[dict] = None,
        params: Optional[dict] = None,
        auth: bool = True,
        extra_headers: Optional[dict] = None,
        timeout: Any = None,
        retry_statuses: frozenset = _RETRYABLE_STATUSES,
        stream: bool = False,
        retry_sent_errors: Optional[bool] = None,
    ) -> httpx.Response:
        url = f"{self._base_url}{path}"
        headers = _request_headers(self._api_key, auth, extra_headers)
        idem_key = headers.get("Idempotency-Key")
        if retry_sent_errors is None:
            retry_sent_errors = not stream

        attempt = 0
        while True:
            request = self._http.build_request(
                method, url, json=json, params=params, headers=headers,
                timeout=timeout if timeout is not None else httpx.USE_CLIENT_DEFAULT,
            )
            try:
                resp = await self._http.send(request, stream=stream)
            except httpx.TransportError as e:
                retryable = isinstance(e, (httpx.NetworkError, httpx.TimeoutException)) and (
                    retry_sent_errors or isinstance(e, _NOT_SENT_ERRORS)
                )
                if retryable and attempt < self._max_retries:
                    delay = _backoff_delay(attempt)
                    logger.debug(
                        "Network error %s on %s %s (attempt %d/%d), retrying in %.2fs",
                        e.__class__.__name__, method, path, attempt + 1,
                        self._max_retries + 1, delay,
                    )
                    await asyncio.sleep(delay)
                    attempt += 1
                    continue
                raise _network_error(e, attempt, idem_key) from e

            if resp.status_code in retry_statuses and attempt < self._max_retries:
                delay = _retry_delay(resp, attempt)
                await resp.aclose()
                logger.debug(
                    "HTTP %d on %s %s (attempt %d/%d), retrying in %.2fs",
                    resp.status_code, method, path, attempt + 1,
                    self._max_retries + 1, delay,
                )
                await asyncio.sleep(delay)
                attempt += 1
                continue

            if not resp.is_success:
                if stream:
                    try:
                        await resp.aread()
                    finally:
                        await resp.aclose()
                message, cls, body = _parse_error(resp)
                raise cls(
                    message, status_code=resp.status_code, response_body=body,
                    idempotency_key=idem_key,
                )
            return resp

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: Optional[dict] = None,
        params: Optional[dict] = None,
        auth: bool = True,
        extra_headers: Optional[dict] = None,
        timeout: Any = None,
        retry_statuses: frozenset = _RETRYABLE_STATUSES,
        retry_sent_errors: bool = True,
    ) -> Any:
        resp = await self._send(
            method, path, json=json, params=params, auth=auth,
            extra_headers=extra_headers, timeout=timeout,
            retry_statuses=retry_statuses, retry_sent_errors=retry_sent_errors,
        )
        if resp.status_code == 204 or not resp.content:
            return None
        return resp.json()

    async def close(self) -> None:
        await self._http.aclose()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.close()


def _network_error(
    e: Exception, attempt: int, idem_key: Optional[str],
) -> errors.NetworkError:
    message = f"Network error after {attempt + 1} attempts: {e}"
    if idem_key and not isinstance(e, _NOT_SENT_ERRORS):
        message += (
            f". The request may still be running on the server: repeat the call with "
            f"idempotency_key={idem_key!r} to get its result without a second charge"
        )
    return errors.NetworkError(message, idempotency_key=idem_key)


def _request_headers(api_key: str, auth: bool, extra_headers: Optional[dict]) -> dict[str, str]:
    headers: dict[str, str] = {}
    if auth:
        headers["Authorization"] = f"Bearer {api_key}"
    if extra_headers:
        headers.update(extra_headers)
    return headers


def _long_timeout(client_timeout: Any, timeout: Optional[float]) -> httpx.Timeout:
    """Таймаут для долгих синхронных /v1-запросов: чтение — `timeout` или не
    меньше DEFAULT_LONG_REQUEST_TIMEOUT, подключение — обычный таймаут клиента."""
    if isinstance(client_timeout, httpx.Timeout):
        connect, base = client_timeout.connect, client_timeout.read
    else:
        connect = base = client_timeout
    if timeout is not None:
        read = timeout
    elif base is None:
        read = None  # клиент без таймаута — так и оставляем
    else:
        read = max(base, DEFAULT_LONG_REQUEST_TIMEOUT)
    if connect is not None and read is not None:
        connect = min(connect, read)
    return httpx.Timeout(read, connect=connect)
