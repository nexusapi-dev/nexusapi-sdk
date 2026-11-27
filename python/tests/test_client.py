"""Smoke + critical-path tests for nexusapi-sdk.

Использует httpx.MockTransport — не делает реальных сетевых запросов.
Проверяет основной flow + retry + idempotency + error mapping.

Запуск: `pytest tests/` из SDKs/python/.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any, Callable

import httpx
import pytest

from nexusapi import (
    AsyncNexusClient,
    AuthenticationError,
    BillingError,
    NetworkError,
    NexusClient,
    NotFoundError,
    RateLimitError,
    TaskFailedError,
    ValidationError,
)


# ============================================================
# Helpers
# ============================================================


def _make_mock_transport(
    handler: Callable[[httpx.Request], httpx.Response],
) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


def _make_async_mock_transport(
    handler: Callable[[httpx.Request], httpx.Response],
) -> httpx.MockTransport:
    # MockTransport works for both sync and async; httpx routes correctly.
    return httpx.MockTransport(handler)


def _ok(body: dict, status: int = 200) -> httpx.Response:
    return httpx.Response(status, json=body)


# ============================================================
# Construction
# ============================================================


def test_client_requires_api_key():
    with pytest.raises(AuthenticationError):
        NexusClient(api_key="")


def test_async_client_requires_api_key():
    with pytest.raises(AuthenticationError):
        AsyncNexusClient(api_key="")


def test_base_url_trailing_slash_stripped():
    client = NexusClient(api_key="k", base_url="https://api.test/")
    assert client._base_url == "https://api.test"


# ============================================================
# Sync — basic videos.create + Task.wait flow
# ============================================================


def test_videos_create_returns_pending_task():
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["headers"] = dict(request.headers)
        captured["body"] = json.loads(request.content)
        return _ok({"task_id": "t-123"})

    client = NexusClient(
        api_key="k", base_url="https://api.test",
        transport=_make_mock_transport(handler),
    )
    task = client.videos.create(model="veo-3-fast", prompt="cat", duration=8)
    assert task.id == "t-123"
    assert task.status == "pending"
    assert task.model == "veo-3-fast"
    assert captured["url"] == "https://api.test/generate"
    assert captured["body"]["params"] == {
        "model_name": "veo-3-fast", "prompt": "cat", "duration": 8,
    }
    # Auto-generated Idempotency-Key (must be present, format sdk-<uuid>)
    assert "idempotency-key" in {k.lower() for k in captured["headers"]}


def test_videos_create_uses_user_idempotency_key():
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["idem"] = request.headers.get("idempotency-key")
        return _ok({"task_id": "t-1"})

    client = NexusClient(
        api_key="k", transport=_make_mock_transport(handler),
    )
    client.videos.create(
        model="m", prompt="p", idempotency_key="user-provided-key",
    )
    assert captured["idem"] == "user-provided-key"


def test_videos_create_forwards_extras():
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return _ok({"task_id": "t-1"})

    client = NexusClient(api_key="k", transport=_make_mock_transport(handler))
    client.videos.create(
        model="m", prompt="p",
        webhook_url="https://x.com/cb",
        negative_prompt="bad",
        unknown_field=42,
    )
    p = captured["body"]["params"]
    assert p["webhook_url"] == "https://x.com/cb"
    assert p["negative_prompt"] == "bad"
    assert p["unknown_field"] == 42


def test_extras_with_none_are_dropped():
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return _ok({"task_id": "t-1"})

    client = NexusClient(api_key="k", transport=_make_mock_transport(handler))
    client.videos.create(
        model="m", prompt="p",
        image_url=None,  # должно НЕ попасть в payload
    )
    assert "image_url" not in captured["body"]["params"]


def test_task_wait_polls_until_done():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return _ok({"task_id": "t-1"})
        # GET /tasks/t-1
        calls["n"] += 1
        if calls["n"] < 3:
            return _ok({"task_id": "t-1", "status": "processing"})
        return _ok({
            "task_id": "t-1", "status": "completed",
            "result": {"video_url": "https://s3/x.mp4"},
        })

    client = NexusClient(api_key="k", transport=_make_mock_transport(handler))
    task = client.videos.create(model="m", prompt="p")
    task.wait(timeout=10, poll_interval=0.01)
    assert task.status == "completed"
    assert task.video_url == "https://s3/x.mp4"
    assert calls["n"] == 3


def test_task_wait_raises_task_failed():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return _ok({"task_id": "t-1"})
        return _ok({
            "task_id": "t-1", "status": "failed",
            "error": "provider down",
        })

    client = NexusClient(api_key="k", transport=_make_mock_transport(handler))
    task = client.videos.create(model="m", prompt="p")
    with pytest.raises(TaskFailedError) as excinfo:
        task.wait(timeout=5, poll_interval=0.01)
    assert "provider down" in str(excinfo.value)
    assert excinfo.value.response_body["task_id"] == "t-1"


# ============================================================
# Sync — error mapping
# ============================================================


@pytest.mark.parametrize("status,exc_cls", [
    (401, AuthenticationError),
    (402, BillingError),
    (404, NotFoundError),
    (422, ValidationError),
    (400, ValidationError),
])
def test_http_status_maps_to_exception(status: int, exc_cls: type):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"detail": "nope"})

    client = NexusClient(
        api_key="k", transport=_make_mock_transport(handler), max_retries=0,
    )
    with pytest.raises(exc_cls) as excinfo:
        client.videos.create(model="m", prompt="p")
    assert excinfo.value.status_code == status


def test_openai_shape_error_parsed():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": {"message": "model not found"}})

    client = NexusClient(
        api_key="k", transport=_make_mock_transport(handler), max_retries=0,
    )
    with pytest.raises(NotFoundError) as excinfo:
        client.tasks.get("x")
    assert "model not found" in str(excinfo.value)


def test_pydantic_validation_array_parsed():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(422, json={
            "detail": [{"loc": ["body", "params", "prompt"], "msg": "field required"}],
        })

    client = NexusClient(
        api_key="k", transport=_make_mock_transport(handler), max_retries=0,
    )
    with pytest.raises(ValidationError) as excinfo:
        client.videos.create(model="m", prompt="x")
    assert "params.prompt" in str(excinfo.value)
    assert "field required" in str(excinfo.value)


# ============================================================
# Sync — retries
# ============================================================


def test_retry_on_503_then_success():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(503)
        return _ok({"task_id": "t-1"})

    client = NexusClient(
        api_key="k", transport=_make_mock_transport(handler), max_retries=3,
    )
    # Patch sleep so test runs instantly
    import nexusapi.client as cm
    orig_sleep = cm.time.sleep
    cm.time.sleep = lambda _: None
    try:
        task = client.videos.create(model="m", prompt="p")
    finally:
        cm.time.sleep = orig_sleep
    assert task.id == "t-1"
    assert calls["n"] == 3


def test_no_retry_on_4xx():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(401, json={"detail": "bad key"})

    client = NexusClient(
        api_key="k", transport=_make_mock_transport(handler), max_retries=3,
    )
    with pytest.raises(AuthenticationError):
        client.videos.create(model="m", prompt="p")
    assert calls["n"] == 1  # No retry on 401


def test_retries_exhausted_raises_with_status():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(503, json={"detail": "service down"})

    client = NexusClient(
        api_key="k", transport=_make_mock_transport(handler), max_retries=2,
    )
    import nexusapi.client as cm
    orig_sleep = cm.time.sleep
    cm.time.sleep = lambda _: None
    try:
        with pytest.raises(Exception) as excinfo:
            client.videos.create(model="m", prompt="p")
    finally:
        cm.time.sleep = orig_sleep
    assert calls["n"] == 3  # initial + 2 retries
    assert excinfo.value.status_code == 503


def test_network_error_retries_then_raises():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        raise httpx.ConnectError("connection refused")

    client = NexusClient(
        api_key="k", transport=_make_mock_transport(handler), max_retries=2,
    )
    import nexusapi.client as cm
    orig_sleep = cm.time.sleep
    cm.time.sleep = lambda _: None
    try:
        with pytest.raises(NetworkError):
            client.tasks.get("x")
    finally:
        cm.time.sleep = orig_sleep
    assert calls["n"] == 3


# ============================================================
# Sync — context manager
# ============================================================


def test_context_manager_closes_http():
    def handler(request: httpx.Request) -> httpx.Response:
        return _ok({"models": []})

    client = NexusClient(api_key="k", transport=_make_mock_transport(handler))
    with client as c:
        assert c is client
    # Calling on a closed client raises
    with pytest.raises(Exception):
        client.tasks.get("x")


# ============================================================
# Async — mirror of key sync tests
# ============================================================


@pytest.mark.asyncio
async def test_async_videos_create():
    def handler(request: httpx.Request) -> httpx.Response:
        return _ok({"task_id": "t-async"})

    async with AsyncNexusClient(
        api_key="k", transport=_make_async_mock_transport(handler),
    ) as client:
        task = await client.videos.create(model="m", prompt="p", duration=8)
        assert task.id == "t-async"
        assert task.status == "pending"


@pytest.mark.asyncio
async def test_async_task_wait():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return _ok({"task_id": "t-1"})
        calls["n"] += 1
        if calls["n"] < 2:
            return _ok({"task_id": "t-1", "status": "processing"})
        return _ok({
            "task_id": "t-1", "status": "completed",
            "result": {"image_urls": ["https://s3/x.png"]},
        })

    async with AsyncNexusClient(
        api_key="k", transport=_make_async_mock_transport(handler),
    ) as client:
        task = await client.images.generate(
            model="m", prompt="p", wait=False,
        )
        await task.wait(timeout=10, poll_interval=0.01)
        assert task.status == "completed"
        assert task.image_url == "https://s3/x.png"
        assert task.image_urls == ["https://s3/x.png"]


@pytest.mark.asyncio
async def test_async_retry_on_503():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 2:
            return httpx.Response(503)
        return _ok({"task_id": "t-1"})

    async with AsyncNexusClient(
        api_key="k", transport=_make_async_mock_transport(handler), max_retries=3,
    ) as client:
        import nexusapi.client as cm
        orig_sleep = cm.asyncio.sleep
        async def fake_sleep(_): return None
        cm.asyncio.sleep = fake_sleep
        try:
            task = await client.videos.create(model="m", prompt="p")
        finally:
            cm.asyncio.sleep = orig_sleep
    assert task.id == "t-1"
    assert calls["n"] == 2


@pytest.mark.asyncio
async def test_async_error_mapping():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"detail": "slow down"})

    async with AsyncNexusClient(
        api_key="k", transport=_make_async_mock_transport(handler), max_retries=0,
    ) as client:
        with pytest.raises(RateLimitError) as excinfo:
            await client.tasks.get("x")
        assert excinfo.value.status_code == 429


# ============================================================
# Models endpoint — no auth required
# ============================================================


def test_models_list_no_auth():
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["auth"] = request.headers.get("authorization")
        return _ok([{"id": "veo-3-fast", "name": "Veo 3 Fast"}])

    client = NexusClient(api_key="k", transport=_make_mock_transport(handler))
    models = client.models.list()
    assert models == [{"id": "veo-3-fast", "name": "Veo 3 Fast"}]
    assert captured["auth"] is None  # No Authorization header on /public/models
