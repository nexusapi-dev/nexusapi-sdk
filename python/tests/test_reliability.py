"""0.3.1: надёжность — Retry-After, 409 после собственного повтора, таймауты
/generate, ожидание задач, разбор нестандартных ответов, проверка входных данных.
"""
from __future__ import annotations

import email.utils
import json
import time
from typing import Any, Callable

import httpx
import pytest

import nexusapi.client as cm
from nexusapi import (
    APIResponseValidationError,
    AsyncNexusClient,
    ConflictError,
    NetworkError,
    NexusClient,
    NexusError,
    NexusTimeoutError,
    NotFoundError,
    ValidationError,
)
from nexusapi.client import _parse_retry_after


def _client(handler: Callable[[httpx.Request], httpx.Response], **kw: Any) -> NexusClient:
    return NexusClient(api_key="k", base_url="https://api.test", transport=httpx.MockTransport(handler), **kw)


def _aclient(handler: Callable[[httpx.Request], httpx.Response], **kw: Any) -> AsyncNexusClient:
    return AsyncNexusClient(api_key="k", base_url="https://api.test", transport=httpx.MockTransport(handler), **kw)


@pytest.fixture
def sleeps(monkeypatch):
    calls: list[float] = []
    monkeypatch.setattr(cm.time, "sleep", lambda s: calls.append(s))

    async def fake_sleep(s):
        calls.append(s)

    monkeypatch.setattr(cm.asyncio, "sleep", fake_sleep)
    return calls


# ============================================================
# Retry-After
# ============================================================


@pytest.mark.parametrize("headers,expected", [
    ({"retry-after": "2"}, 2.0),
    ({"retry-after": "0"}, 0.0),
    ({"retry-after-ms": "1500"}, 1.5),
    ({"retry-after-ms": "250", "retry-after": "9"}, 0.25),
    ({"retry-after": "-1"}, None),
    ({"retry-after": "nan"}, None),
    ({"retry-after": "inf"}, None),
    ({"retry-after": "3600"}, None),
    ({"retry-after": "soon"}, None),
    ({}, None),
])
def test_parse_retry_after(headers, expected):
    assert _parse_retry_after(httpx.Headers(headers)) == expected


def test_parse_retry_after_http_date():
    date = email.utils.formatdate(time.time() + 10, usegmt=True)
    value = _parse_retry_after(httpx.Headers({"retry-after": date}))
    assert value is not None and 5 <= value <= 11


def test_bad_retry_after_falls_back_to_backoff(sleeps):
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(503, headers={"retry-after": "-1"})
        if calls["n"] == 2:
            return httpx.Response(503, headers={"retry-after": "86400"})
        return httpx.Response(200, json={"task_id": "t-1"})

    assert _client(handler).videos.create(model="m", prompt="p").id == "t-1"
    assert len(sleeps) == 2 and all(0 < s <= 5 for s in sleeps)


# ============================================================
# 409 после собственного повтора SDK
# ============================================================


def test_conflict_after_own_retry_waits_for_stored_response(sleeps):
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers["idempotency-key"])
        if len(seen) == 1:
            raise httpx.ReadTimeout("slow preflight")
        if len(seen) < 4:
            return httpx.Response(409, json={"detail": "still being processed"})
        return httpx.Response(202, json={"task_id": "t-1"})

    task = _client(handler).videos.create(model="m", prompt="p")
    assert task.id == "t-1"
    assert len(set(seen)) == 1 and len(seen) == 4


def test_conflict_on_first_attempt_is_raised(sleeps):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json={"detail": "still being processed"})

    with pytest.raises(ConflictError) as excinfo:
        _client(handler).videos.create(model="m", prompt="p", idempotency_key="order-12345")
    assert excinfo.value.idempotency_key == "order-12345"
    assert sleeps == []


def test_conflict_wait_has_a_budget(sleeps, monkeypatch):
    clock = {"t": 0.0}
    monkeypatch.setattr(cm.time, "monotonic", lambda: clock["t"])
    monkeypatch.setattr(cm.time, "sleep", lambda s: clock.__setitem__("t", clock["t"] + s))
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ReadTimeout("slow")
        return httpx.Response(409, json={"detail": "still being processed"})

    with pytest.raises(ConflictError):
        _client(handler).videos.create(model="m", prompt="p")
    assert clock["t"] <= cm._CONFLICT_WAIT_BUDGET + 10


@pytest.mark.asyncio
async def test_async_conflict_after_own_retry(sleeps):
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ReadTimeout("slow")
        if calls["n"] == 2:
            return httpx.Response(409, json={"detail": "still being processed"})
        return httpx.Response(202, json={"task_id": "t-1"})

    async with _aclient(handler) as client:
        task = await client.videos.create(model="m", prompt="p")
    assert task.id == "t-1"


# ============================================================
# Таймауты /generate и ожидание задач
# ============================================================


def test_generate_read_timeout_is_at_least_120s():
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["timeout"] = request.extensions.get("timeout")
        return httpx.Response(202, json={"task_id": "t-1"})

    _client(handler).videos.create(model="m", prompt="p")
    assert captured["timeout"]["read"] == 120.0
    assert captured["timeout"]["connect"] == 60.0


def test_image_wait_default_covers_slow_models():
    assert cm.DEFAULT_IMAGE_WAIT_TIMEOUT >= 540


def test_wait_survives_transient_poll_errors(sleeps):
    polls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(202, json={"task_id": "t-1"})
        polls["n"] += 1
        if polls["n"] <= 3:
            return httpx.Response(502, text="Bad Gateway")
        return httpx.Response(200, json={
            "task_id": "t-1", "status": "completed", "result": {"video_url": "https://s3/v.mp4"},
        })

    task = _client(handler, max_retries=0).videos.create(model="m", prompt="p")
    task.wait(timeout=60, poll_interval=0.01)
    assert task.video_url == "https://s3/v.mp4"


def test_wait_timeout_keeps_the_task(sleeps, monkeypatch):
    clock = {"t": 0.0}
    monkeypatch.setattr(cm.time, "monotonic", lambda: clock["t"])
    monkeypatch.setattr(cm.time, "sleep", lambda s: clock.__setitem__("t", clock["t"] + s))

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(202, json={"task_id": "t-1"})
        return httpx.Response(200, json={"task_id": "t-1", "status": "processing"})

    with pytest.raises(NexusTimeoutError) as excinfo:
        _client(handler).images.generate(model="seedream-5.0-pro", prompt="p", timeout=10, poll_interval=3)
    err = excinfo.value
    assert err.task_id == "t-1"
    assert err.task.id == "t-1" and err.task.status == "processing"
    assert clock["t"] <= 10.0  # не «пересыпает» дедлайн


def test_wait_does_not_swallow_permanent_errors(sleeps):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(202, json={"task_id": "t-1"})
        return httpx.Response(404, json={"detail": "Task not found"})

    task = _client(handler).videos.create(model="m", prompt="p")
    with pytest.raises(NotFoundError) as excinfo:
        task.wait(timeout=60, poll_interval=0.01)
    assert excinfo.value.task_id == "t-1"


def test_video_wait_survives_transient_errors(sleeps):
    polls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        polls["n"] += 1
        if polls["n"] == 1:
            raise httpx.ReadError("reset")
        return httpx.Response(200, json={"id": "v-1", "status": "completed", "url": "u"})

    done = _client(handler, max_retries=0).v1.videos.wait("v-1", poll_interval=0.01)
    assert done["url"] == "u"


@pytest.mark.asyncio
async def test_async_wait_survives_transient_poll_errors(sleeps):
    polls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(202, json={"task_id": "t-1"})
        polls["n"] += 1
        if polls["n"] == 1:
            return httpx.Response(503)
        return httpx.Response(200, json={"task_id": "t-1", "status": "completed", "result": {}})

    async with _aclient(handler, max_retries=0) as client:
        task = await client.images.generate(model="m", prompt="p", poll_interval=0.01)
    assert task.status == "completed"


# ============================================================
# Нестандартные ответы
# ============================================================


def test_non_json_success_body():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>maintenance</html>")

    with pytest.raises(APIResponseValidationError) as excinfo:
        _client(handler).tasks.get("t-1")
    assert excinfo.value.status_code == 200
    assert "maintenance" in excinfo.value.response_body


def test_generate_response_without_task_id():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(202, json={"message": "accepted"})

    with pytest.raises(APIResponseValidationError) as excinfo:
        _client(handler).videos.create(model="m", prompt="p", idempotency_key="order-12345")
    assert excinfo.value.idempotency_key == "order-12345"


@pytest.mark.parametrize("body,expected", [
    ({"detail": ["just a string"]}, "just a string"),
    ({"error": "plain error"}, "plain error"),
    ({"detail": {"message": "nested"}}, "nested"),
    ({"detail": [{"loc": ["body", "params", "duration"], "msg": "too long"}]}, "params.duration: too long"),
])
def test_error_parser_handles_odd_bodies(body, expected):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json=body)

    with pytest.raises(ValidationError) as excinfo:
        _client(handler, max_retries=0).tasks.get("t-1")
    assert str(excinfo.value) == expected


def test_long_html_error_is_truncated_and_headers_kept():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, text="<html>" + "x" * 5000 + "</html>", headers={"x-trace": "abc"})

    with pytest.raises(NexusError) as excinfo:
        _client(handler, max_retries=0).tasks.get("t-1")
    assert len(str(excinfo.value)) <= 501
    assert excinfo.value.headers["x-trace"] == "abc"


def test_task_properties_tolerate_non_dict_result():
    task = cm.Task(id="t", status="completed", result=["unexpected"])  # type: ignore[arg-type]
    assert task.video_url is None and task.image_urls == [] and task.audio_url is None
    audio = cm.Task(id="t", status="completed", result={"audio_urls": ["a.mp3", "b.mp3"]})
    assert audio.audio_url == "a.mp3" and audio.audio_urls == ["a.mp3", "b.mp3"]


# ============================================================
# Проверка входных данных
# ============================================================


def test_task_id_is_escaped():
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.raw_path.decode()
        return httpx.Response(200, json={"task_id": "x", "status": "pending"})

    _client(handler).tasks.get("../me/keys?x=1")
    assert captured["path"] == "/tasks/..%2Fme%2Fkeys%3Fx%3D1"


def test_base_url_validation_and_v1_suffix():
    with pytest.raises(ValueError):
        NexusClient(api_key="k", base_url="nexusapi.dev")
    with pytest.warns(UserWarning, match="/v1"):
        client = NexusClient(api_key="k", base_url="https://nexusapi.dev/v1/")
    assert client._base_url == "https://nexusapi.dev"


def test_idempotency_key_length_checked_locally():
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("must not be sent")

    with pytest.raises(ValueError):
        _client(handler).videos.create(model="m", prompt="p", idempotency_key="short")
    with pytest.raises(ValueError):
        _client(handler).chat.completions.create(model="m", messages=[], stream=True, idempotency_key="x" * 300)


def test_stream_network_error_does_not_promise_replay():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow")

    with pytest.raises(NetworkError) as excinfo:
        _client(handler).chat.completions.create(
            model="m", messages=[], stream=True, idempotency_key="stream-key-1",
        )
    assert "cannot be replayed" in str(excinfo.value)
    assert "stored result" not in str(excinfo.value)


def test_unsent_request_message():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    with pytest.raises(NetworkError) as excinfo:
        _client(handler, max_retries=0).chat.completions.create(model="m", messages=[])
    assert str(excinfo.value).startswith("Could not connect")


def test_client_context_manager_is_typed():
    # mypy/pyright: `with NexusClient(...) as c` даёт NexusClient, а не Any.
    import typing

    assert typing.get_type_hints(NexusClient.__enter__)["return"] is NexusClient
    assert typing.get_type_hints(AsyncNexusClient.__aenter__)["return"] is AsyncNexusClient


def test_version_matches_pyproject():
    import pathlib
    import re

    import nexusapi

    text = (pathlib.Path(__file__).resolve().parents[1] / "pyproject.toml").read_text()
    match = re.search(r'^version\s*=\s*"([^"]+)"', text, re.M)
    assert match and nexusapi.__version__ == match.group(1)
