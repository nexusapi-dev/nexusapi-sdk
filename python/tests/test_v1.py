"""Tests for the OpenAI-compatible /v1 resources (0.3.0).

chat.completions (обычный ответ и SSE-стрим), v1.models, v1.images, v1.videos,
политика повторов для долгих запросов. Сеть не используется — httpx.MockTransport.
"""
from __future__ import annotations

import json
from typing import Any, Callable

import httpx
import pytest

import nexusapi.client as cm
from nexusapi import (
    APITimeoutError,
    AsyncNexusClient,
    InternalServerError,
    AsyncStream,
    ConflictError,
    NetworkError,
    NexusClient,
    NexusError,
    NexusTimeoutError,
    NotFoundError,
    Stream,
    StreamError,
    TaskFailedError,
    ValidationError,
)


def _client(handler: Callable[[httpx.Request], httpx.Response], **kw: Any) -> NexusClient:
    return NexusClient(
        api_key="k", base_url="https://api.test",
        transport=httpx.MockTransport(handler), **kw,
    )


def _aclient(handler: Callable[[httpx.Request], httpx.Response], **kw: Any) -> AsyncNexusClient:
    return AsyncNexusClient(
        api_key="k", base_url="https://api.test",
        transport=httpx.MockTransport(handler), **kw,
    )


def _sse(*events: Any, done: bool = True, sep: str = "\n") -> bytes:
    out = []
    for e in events:
        data = e if isinstance(e, str) else json.dumps(e)
        out.append(f"data: {data}{sep}{sep}")
    if done:
        out.append(f"data: [DONE]{sep}{sep}")
    return "".join(out).encode()


def _chunk(content: str) -> dict:
    return {
        "id": "c-1", "object": "chat.completion.chunk", "model": "claude-sonnet-5",
        "choices": [{"index": 0, "delta": {"content": content}, "finish_reason": None}],
    }


COMPLETION = {
    "id": "c-1",
    "object": "chat.completion",
    "model": "claude-sonnet-5",
    "choices": [{"index": 0, "message": {"role": "assistant", "content": "Привет!"},
                 "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
}


@pytest.fixture
def no_sleep(monkeypatch):
    monkeypatch.setattr(cm.time, "sleep", lambda _: None)

    async def fake_sleep(_):
        return None

    monkeypatch.setattr(cm.asyncio, "sleep", fake_sleep)


# ============================================================
# chat.completions — без стрима
# ============================================================


def test_chat_completion_request_shape():
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["headers"] = request.headers
        captured["body"] = json.loads(request.content)
        captured["timeout"] = request.extensions.get("timeout")
        return httpx.Response(200, json=COMPLETION)

    client = _client(handler)
    reply = client.chat.completions.create(
        model="claude-sonnet-5",
        messages=[{"role": "user", "content": "Привет"}],
        max_tokens=100,
        temperature=None,  # None не уходит в тело
        tools=[{"type": "function", "function": {"name": "f"}}],
    )
    assert reply == COMPLETION
    assert captured["url"] == "https://api.test/v1/chat/completions"
    assert captured["headers"]["authorization"] == "Bearer k"
    assert captured["headers"]["idempotency-key"].startswith("sdk-")
    assert captured["body"] == {
        "model": "claude-sonnet-5",
        "messages": [{"role": "user", "content": "Привет"}],
        "max_tokens": 100,
        "tools": [{"type": "function", "function": {"name": "f"}}],
    }
    # Долгий запрос: чтение не меньше 600 с, подключение — обычный таймаут клиента.
    assert captured["timeout"]["read"] == 600.0
    assert captured["timeout"]["connect"] == 60.0


def test_chat_per_call_timeout_and_user_idempotency_key():
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["timeout"] = request.extensions.get("timeout")
        captured["idem"] = request.headers.get("idempotency-key")
        return httpx.Response(200, json=COMPLETION)

    client = _client(handler, timeout=900)
    client.chat.completions.create(
        model="m", messages=[], idempotency_key="my-key-123", timeout=30,
    )
    assert captured["idem"] == "my-key-123"
    assert captured["timeout"]["read"] == 30
    assert captured["timeout"]["connect"] == 30


def test_chat_client_timeout_above_default_is_kept():
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["timeout"] = request.extensions.get("timeout")
        return httpx.Response(200, json=COMPLETION)

    _client(handler, timeout=900).chat.completions.create(model="m", messages=[])
    assert captured["timeout"]["read"] == 900


def test_chat_openai_error_shape():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": {
            "message": "Model 'x' is not supported by /v1/chat/completions",
            "type": "not_found_error",
        }})

    with pytest.raises(NotFoundError) as excinfo:
        _client(handler).chat.completions.create(model="x", messages=[])
    assert "not supported" in str(excinfo.value)
    assert excinfo.value.response_body["error"]["type"] == "not_found_error"


def test_chat_retry_policy(no_sleep):
    """Чат: 502/503/504 повторяем (прокси, сервер ещё работает), 500 — нет:
    он мог прийти после вызова модели, а чат сервер к ключу не привязывает."""
    for status, expected_calls in ((503, 3), (502, 3), (504, 3), (500, 1)):
        calls = {"n": 0}
        keys: set[str] = set()

        def handler(request: httpx.Request, status=status) -> httpx.Response:
            calls["n"] += 1
            keys.add(request.headers["idempotency-key"])
            return httpx.Response(status, json={"error": {"message": "x"}})

        with pytest.raises(NexusError) as excinfo:
            _client(handler, max_retries=2).chat.completions.create(model="m", messages=[])
        assert excinfo.value.status_code == status
        assert calls["n"] == expected_calls, status
        assert len(keys) == 1


def test_chat_retry_reuses_idempotency_key(no_sleep):
    keys: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        keys.append(request.headers["idempotency-key"])
        if len(keys) == 1:
            return httpx.Response(429, json={"error": {"message": "slow down"}})
        return httpx.Response(200, json=COMPLETION)

    _client(handler).chat.completions.create(model="m", messages=[])
    assert len(keys) == 2 and keys[0] == keys[1]


def test_conflict_maps_to_conflict_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json={"detail": "still being processed"})

    with pytest.raises(ConflictError):
        _client(handler).chat.completions.create(model="m", messages=[])


# ============================================================
# chat.completions — стрим
# ============================================================


def test_chat_stream_yields_chunks_and_closes():
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        captured["headers"] = request.headers
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"},
            content=b": keep-alive comment\n\n" + _sse(_chunk("При"), _chunk("вет")),
        )

    client = _client(handler)
    stream = client.chat.completions.create(model="m", messages=[], stream=True)
    assert isinstance(stream, Stream)
    with stream:
        text = "".join(c["choices"][0]["delta"]["content"] for c in stream)
    assert text == "Привет"
    assert captured["body"]["stream"] is True
    # Для стрима ключ идемпотентности не генерируется автоматически.
    assert "idempotency-key" not in captured["headers"]
    assert captured["headers"]["accept"] == "text/event-stream"
    assert stream.response.is_closed


def test_chat_stream_crlf_and_split_chunks():
    """События, разрезанные на произвольные куски, и переводы строк \\r\\n."""
    raw = _sse(_chunk("a"), _chunk("b"), sep="\r\n")

    class Chunked(httpx.SyncByteStream):
        def __iter__(self):
            for i in range(0, len(raw), 7):
                yield raw[i:i + 7]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=Chunked())

    stream = _client(handler).chat.completions.create(model="m", messages=[], stream=True)
    assert [c["choices"][0]["delta"]["content"] for c in stream] == ["a", "b"]


def test_chat_stream_without_done_ends_cleanly():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b'data: {"choices": []}')

    stream = _client(handler).chat.completions.create(model="m", messages=[], stream=True)
    assert list(stream) == [{"choices": []}]


def test_chat_stream_server_error_event_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_sse(_chunk("a"), {"error": {
            "message": "Text provider could not complete this request. No funds were charged.",
            "type": "upstream_error", "code": "text_generation_failed",
        }}))

    stream = _client(handler).chat.completions.create(model="m", messages=[], stream=True)
    got = []
    with pytest.raises(StreamError) as excinfo:
        for c in stream:
            got.append(c)
    assert len(got) == 1
    assert "No funds were charged" in str(excinfo.value)
    assert excinfo.value.response_body["error"]["code"] == "text_generation_failed"
    assert stream.response.is_closed


def test_chat_stream_http_error_before_stream():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": {"message": "bad sampling"}})

    with pytest.raises(ValidationError) as excinfo:
        _client(handler).chat.completions.create(model="m", messages=[], stream=True)
    assert "bad sampling" in str(excinfo.value)


def test_chat_stream_user_idempotency_key_is_sent():
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["idem"] = request.headers.get("idempotency-key")
        return httpx.Response(200, content=_sse())

    stream = _client(handler).chat.completions.create(
        model="m", messages=[], stream=True, idempotency_key="stream-key-1",
    )
    assert list(stream) == []
    assert captured["idem"] == "stream-key-1"


def test_chat_stream_retries_only_unsent_requests(no_sleep):
    # ConnectError — запрос не ушёл, повторяем.
    calls = {"n": 0}

    def flaky_connect(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectError("refused")
        return httpx.Response(200, content=_sse(_chunk("ok")))

    stream = _client(flaky_connect).chat.completions.create(model="m", messages=[], stream=True)
    assert [c["choices"][0]["delta"]["content"] for c in stream] == ["ok"]
    assert calls["n"] == 2

    # ReadTimeout — сервер мог начать генерацию, не повторяем.
    calls["n"] = 0

    def read_timeout(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        raise httpx.ReadTimeout("slow")

    with pytest.raises(NetworkError):
        _client(read_timeout).chat.completions.create(model="m", messages=[], stream=True)
    assert calls["n"] == 1


def test_chat_stream_interrupted_raises_network_error():
    class Broken(httpx.SyncByteStream):
        def __iter__(self):
            yield _sse(_chunk("a"), done=False)
            raise httpx.ReadError("connection reset")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=Broken())

    stream = _client(handler).chat.completions.create(model="m", messages=[], stream=True)
    assert next(stream)["choices"][0]["delta"]["content"] == "a"
    with pytest.raises(NetworkError):
        next(stream)


# ============================================================
# v1.models / v1.images / v1.videos
# ============================================================


def test_v1_chat_is_same_object():
    client = _client(lambda r: httpx.Response(200, json={}))
    assert client.v1.chat is client.chat


def test_v1_models_list_requires_auth():
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json={"object": "list", "data": [
            {"id": "veo-3-fast", "object": "model", "created": 1700000000, "owned_by": "nexus"},
        ]})

    models = _client(handler).v1.models.list()
    assert [m["id"] for m in models] == ["veo-3-fast"]
    assert captured["url"] == "https://api.test/v1/models"
    assert captured["auth"] == "Bearer k"


def test_v1_images_generate():
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        captured["idem"] = request.headers.get("idempotency-key")
        captured["timeout"] = request.extensions.get("timeout")
        return httpx.Response(200, json={"created": 1, "data": [{"url": "https://s3/x.png"}]})

    resp = _client(handler).v1.images.generate(
        model="nano-banana", prompt="cat", size="1024x1024",
        image_url="https://example.com/in.png", quality=None,
    )
    assert resp["data"][0]["url"] == "https://s3/x.png"
    assert captured["url"] == "https://api.test/v1/images/generations"
    assert captured["body"] == {
        "model": "nano-banana", "prompt": "cat", "size": "1024x1024",
        "image_url": "https://example.com/in.png",
    }
    assert captured["idem"].startswith("sdk-")
    assert captured["timeout"]["read"] == 600.0


def test_v1_images_504_retries_with_same_key_and_exposes_task_id(no_sleep):
    """504 у /v1/images: задача идёт и привязана к ключу — повтор с тем же
    ключом продолжает ждать её; если ждать уже нельзя, в ошибке task_id."""
    keys: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        keys.append(request.headers["idempotency-key"])
        return httpx.Response(504, headers={"x-request-id": "req_abc"}, json={"error": {
            "message": "Task t-9 did not complete within 180s", "type": "timeout_error",
            "task_id": "t-9",
        }})

    with pytest.raises(InternalServerError) as excinfo:
        _client(handler, max_retries=2).v1.images.generate(model="m", prompt="p")
    err = excinfo.value
    assert err.status_code == 504 and err.task_id == "t-9"
    assert err.type == "timeout_error" and err.request_id == "req_abc"
    assert len(keys) == 3 and len(set(keys)) == 1

    calls = {"n": 0}

    def eventually(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(504, json={"error": {"message": "wait", "task_id": "t-9"}})
        return httpx.Response(200, json={"created": 1, "data": [{"url": "u"}]})

    assert _client(eventually).v1.images.generate(model="m", prompt="p")["data"][0]["url"] == "u"


def test_v1_videos_create_retrieve_wait():
    calls = {"get": 0}
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            captured["url"] = str(request.url)
            captured["body"] = json.loads(request.content)
            captured["idem"] = request.headers.get("idempotency-key")
            return httpx.Response(200, json={
                "id": "v-1", "object": "video", "model": "veo-3-fast",
                "status": "queued", "progress": 0, "created_at": 1,
            })
        captured["get_url"] = str(request.url)
        calls["get"] += 1
        if calls["get"] < 3:
            return httpx.Response(200, json={"id": "v-1", "status": "in_progress", "progress": 50})
        return httpx.Response(200, json={
            "id": "v-1", "status": "completed", "progress": 100, "url": "https://s3/v.mp4",
        })

    client = _client(handler)
    video = client.v1.videos.create(
        model="veo-3-fast", prompt="cat", seconds=8, size="1920x1080",
        input_reference="https://example.com/frame.jpg", resolution="1080p",
    )
    assert video["status"] == "queued"
    assert captured["url"] == "https://api.test/v1/videos"
    assert captured["body"] == {
        "model": "veo-3-fast", "prompt": "cat", "seconds": 8, "size": "1920x1080",
        "input_reference": "https://example.com/frame.jpg", "resolution": "1080p",
    }
    assert captured["idem"].startswith("sdk-")

    done = client.v1.videos.wait(video, timeout=10, poll_interval=0.001)
    assert done["url"] == "https://s3/v.mp4"
    assert calls["get"] == 3
    assert captured["get_url"] == "https://api.test/v1/videos/v-1"

    assert client.v1.videos.retrieve("v-1")["status"] == "completed"


def test_v1_videos_path_is_escaped():
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.raw_path.decode()
        return httpx.Response(200, json={"id": "x", "status": "queued"})

    _client(handler).v1.videos.retrieve("../me/keys")
    assert captured["path"] == "/v1/videos/..%2Fme%2Fkeys"


def test_v1_videos_wait_failed_and_timeout():
    def failed(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "id": "v-1", "status": "failed",
            "error": {"message": "provider down", "type": "api_error"},
        })

    with pytest.raises(TaskFailedError) as excinfo:
        _client(failed).v1.videos.wait("v-1", poll_interval=0.001)
    assert "provider down" in str(excinfo.value)
    assert excinfo.value.response_body["task_id"] == "v-1"

    def pending(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id": "v-1", "status": "queued"})

    with pytest.raises(NexusTimeoutError):
        _client(pending).v1.videos.wait("v-1", timeout=0.01, poll_interval=0.001)


# ============================================================
# Async
# ============================================================


@pytest.mark.asyncio
async def test_async_chat_completion():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/chat/completions"
        assert request.headers["idempotency-key"].startswith("sdk-")
        return httpx.Response(200, json=COMPLETION)

    async with _aclient(handler) as client:
        reply = await client.chat.completions.create(model="m", messages=[])
    assert reply["choices"][0]["message"]["content"] == "Привет!"


@pytest.mark.asyncio
async def test_async_chat_stream():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_sse(_chunk("a"), _chunk("b")))

    async with _aclient(handler) as client:
        stream = await client.chat.completions.create(model="m", messages=[], stream=True)
        assert isinstance(stream, AsyncStream)
        async with stream:
            parts = [c["choices"][0]["delta"]["content"] async for c in stream]
    assert parts == ["a", "b"]
    assert stream.response.is_closed


@pytest.mark.asyncio
async def test_async_chat_stream_error_event():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_sse({"error": {"message": "boom"}}))

    async with _aclient(handler) as client:
        stream = await client.chat.completions.create(model="m", messages=[], stream=True)
        with pytest.raises(StreamError):
            async for _ in stream:
                pass


@pytest.mark.asyncio
async def test_async_v1_resources(no_sleep):
    calls = {"get": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/v1/models":
            return httpx.Response(200, json={"object": "list", "data": [{"id": "m"}]})
        if path == "/v1/images/generations":
            return httpx.Response(200, json={"created": 1, "data": [{"url": "u"}]})
        if request.method == "POST" and path == "/v1/videos":
            return httpx.Response(200, json={"id": "v-1", "status": "queued"})
        calls["get"] += 1
        status = "completed" if calls["get"] > 1 else "in_progress"
        return httpx.Response(200, json={"id": "v-1", "status": status, "url": "v"})

    async with _aclient(handler) as client:
        assert client.v1.chat is client.chat
        assert [m["id"] for m in await client.v1.models.list()] == ["m"]
        img = await client.v1.images.generate(model="m", prompt="p")
        assert img["data"][0]["url"] == "u"
        video = await client.v1.videos.create(model="m", prompt="p")
        done = await client.v1.videos.wait(video["id"], poll_interval=0)
        assert done["status"] == "completed"
        assert calls["get"] == 2


# ============================================================
# Обрыв долгого запроса после отправки: без автоповтора, ключ в ошибке
# ============================================================


@pytest.mark.parametrize("exc,err_cls", [
    (httpx.ReadError("reset"), NetworkError),
    (httpx.ReadTimeout("slow"), APITimeoutError),
])
def test_long_request_retried_after_send_with_same_key(no_sleep, exc, err_cls):
    """Обрыв после отправки: сервер хранит результат по ключу (генерации — ту же
    задачу), поэтому SDK повторяет с тем же ключом."""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.headers["idempotency-key"])
        raise exc

    for call in (
        lambda c: c.chat.completions.create(model="m", messages=[]),
        lambda c: c.v1.images.generate(model="m", prompt="p"),
    ):
        calls.clear()
        with pytest.raises(err_cls) as excinfo:
            call(_client(handler, max_retries=2))
        assert len(calls) == 3 and len(set(calls)) == 1
        assert excinfo.value.idempotency_key == calls[0]
        assert calls[0] in str(excinfo.value)


def test_long_request_retried_when_not_sent(no_sleep):
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectError("refused")
        return httpx.Response(200, json=COMPLETION)

    assert _client(handler).chat.completions.create(model="m", messages=[]) == COMPLETION
    assert calls["n"] == 2


def test_short_post_still_retried_after_send(no_sleep):
    """Нативный /generate и /v1/videos быстрые — повтор после обрыва как в 0.2.0."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ReadError("reset")
        return httpx.Response(200, json={"task_id": "t-1"})

    assert _client(handler).videos.create(model="m", prompt="p").id == "t-1"
    assert calls["n"] == 2


def test_errors_carry_idempotency_key():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json={"detail": "still being processed"})

    with pytest.raises(ConflictError) as excinfo:
        _client(handler).chat.completions.create(model="m", messages=[], idempotency_key="k-12345678")
    assert excinfo.value.idempotency_key == "k-12345678"


def test_protocol_error_wrapped_as_network_error():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.RemoteProtocolError("Server disconnected without sending a response")

    with pytest.raises(NetworkError):
        _client(handler, max_retries=0).tasks.get("t-1")


@pytest.mark.asyncio
async def test_async_long_request_retried_after_send(no_sleep):
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ReadError("reset")
        return httpx.Response(200, json={"created": 1, "data": [{"url": "u"}]})

    async with _aclient(handler, max_retries=3) as client:
        resp = await client.v1.images.generate(model="m", prompt="p")
    assert resp["data"][0]["url"] == "u"
    assert calls["n"] == 2


