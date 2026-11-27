"""0.4.0: конфигурация (env, with_options, http_client), иерархия ошибок и
request_id, подпись вебхуков, сериализация задач."""
from __future__ import annotations

import hashlib
import hmac
import json
import pickle
import time
from typing import Any, Callable

import httpx
import pytest

import nexusapi
from nexusapi import (
    APIStatusError,
    APITimeoutError,
    AsyncNexusClient,
    AuthenticationError,
    InternalServerError,
    NetworkError,
    NexusClient,
    NexusError,
    ValidationError,
    WebhookVerificationError,
    webhooks,
)

Handler = Callable[[httpx.Request], httpx.Response]


def _client(handler: Handler, **kw: Any) -> NexusClient:
    return NexusClient(api_key="k-123", base_url="https://api.test", transport=httpx.MockTransport(handler), **kw)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("NEXUSAPI_API_KEY", raising=False)
    monkeypatch.delenv("NEXUSAPI_BASE_URL", raising=False)


# ============================================================
# Конфигурация
# ============================================================


def test_api_key_and_base_url_from_env(monkeypatch):
    monkeypatch.setenv("NEXUSAPI_API_KEY", "env-key-1")
    monkeypatch.setenv("NEXUSAPI_BASE_URL", "https://env.test/")
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers["authorization"]
        return httpx.Response(200, json={"task_id": "t", "status": "pending"})

    NexusClient(transport=httpx.MockTransport(handler)).tasks.get("t")
    assert seen == {"url": "https://env.test/tasks/t", "auth": "Bearer env-key-1"}


def test_missing_key_mentions_env_var():
    with pytest.raises(AuthenticationError, match="NEXUSAPI_API_KEY"):
        NexusClient()
    with pytest.raises(AuthenticationError):
        AsyncNexusClient()


def test_default_headers_user_agent_and_repr():
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(request.headers)
        return httpx.Response(200, json={"task_id": "t", "status": "pending"})

    client = _client(handler, default_headers={"X-Team": "media"})
    client.tasks.get("t")
    assert seen["x-team"] == "media"
    assert seen["user-agent"].startswith(f"nexusapi-python/") and " python/3." in seen["user-agent"]
    assert "k-123" not in repr(client)


def test_with_options_shares_pool_and_overrides():
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append({"timeout": request.extensions.get("timeout"), "team": request.headers.get("x-team"),
                     "trace": request.headers.get("x-trace")})
        return httpx.Response(503) if len(seen) == 1 else httpx.Response(200, json={"task_id": "t", "status": "pending"})

    base = _client(handler, default_headers={"X-Team": "media"})
    quick = base.with_options(timeout=5, max_retries=0, default_headers={"X-Trace": "1"})
    assert quick._http is base._http
    with pytest.raises(InternalServerError):
        quick.tasks.get("t")  # max_retries=0: без повтора
    assert seen[0]["timeout"]["read"] == 5
    assert seen[0]["team"] == "media" and seen[0]["trace"] == "1"
    quick.close()  # копия пул не закрывает
    assert base.tasks.get("t").id == "t"
    base.close()
    assert base._http.is_closed


def test_http_client_is_used_and_not_closed():
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["ua"] = request.headers["user-agent"]
        seen["timeout"] = request.extensions.get("timeout")
        return httpx.Response(200, json={"task_id": "t", "status": "pending"})

    external = httpx.Client(transport=httpx.MockTransport(handler), timeout=999)
    client = NexusClient(api_key="k", base_url="https://api.test", http_client=external, timeout=30)
    client.tasks.get("t")
    assert seen["ua"].startswith("nexusapi-python/")
    assert seen["timeout"]["read"] == 30  # таймаут SDK, а не клиента
    client.close()
    assert not external.is_closed
    external.close()
    with pytest.raises(ValueError):
        NexusClient(api_key="k", http_client=external, transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_async_with_options_and_http_client():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"task_id": "t", "status": "pending"})

    external = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    async with AsyncNexusClient(api_key="k", base_url="https://api.test", http_client=external) as client:
        copy = client.with_options(max_retries=0)
        assert copy._http is external
        assert (await copy.tasks.get("t")).id == "t"
    assert not external.is_closed
    await external.aclose()


# ============================================================
# Ошибки
# ============================================================


@pytest.mark.parametrize("status,cls", [
    (400, ValidationError), (401, AuthenticationError), (424, APIStatusError),
    (500, InternalServerError), (502, InternalServerError),
])
def test_status_error_hierarchy(status, cls):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, headers={"x-request-id": "req_42"}, json={"error": {
            "message": "boom", "type": "invalid_request_error", "code": "bad_size", "param": "size",
        }})

    with pytest.raises(cls) as excinfo:
        _client(handler, max_retries=0).tasks.get("t")
    err = excinfo.value
    assert isinstance(err, APIStatusError) and isinstance(err, NexusError)
    assert (err.request_id, err.code, err.type, err.param) == ("req_42", "bad_size", "invalid_request_error", "size")
    assert "req_42" in repr(err)


def test_timeout_error_is_network_error():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("slow")

    with pytest.raises(APITimeoutError) as excinfo:
        _client(handler, max_retries=0).tasks.get("t")
    assert isinstance(excinfo.value, NetworkError)


# ============================================================
# Вебхуки
# ============================================================


def _reference_secret(api_key: str) -> str:
    # Алгоритм сервера (app/services/webhook_signing.py).
    return "whsec_" + hmac.new(api_key.encode(), b"nexus-webhook-signing-v1", hashlib.sha256).hexdigest()


def _signed(body: bytes, secret: str, ts: int | None = None) -> dict[str, str]:
    ts = int(time.time()) if ts is None else ts
    sig = hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    return {"X-Nexus-Timestamp": str(ts), "X-Nexus-Signature": f"v1={sig}"}


BODY = json.dumps({"task_id": "t-1", "status": "completed", "result": {"video_url": "u"}}).encode()


def test_secret_derivation_matches_server():
    assert webhooks.secret_from_api_key("k-123") == _reference_secret("k-123")


def test_unwrap_valid_signature():
    secret = _reference_secret("k-123")
    event = webhooks.unwrap(BODY, _signed(BODY, secret), secret)
    assert event["task_id"] == "t-1" and event["status"] == "completed"
    # регистр заголовков и str-тело
    lower = {k.lower(): v for k, v in _signed(BODY, secret).items()}
    assert webhooks.unwrap(BODY.decode(), lower, secret)["task_id"] == "t-1"


def test_multiple_signatures_any_matches():
    secret = _reference_secret("k-123")
    headers = _signed(BODY, secret)
    headers["X-Nexus-Signature"] = "v1=deadbeef, " + headers["X-Nexus-Signature"] + ",v0=zzz"
    webhooks.verify_signature(BODY, headers, secret)


@pytest.mark.parametrize("mutate", [
    lambda h, b: (h, b + b" "),  # тело изменено
    lambda h, b: ({**h, "X-Nexus-Signature": "v1=" + "0" * 64}, b),
    lambda h, b: ({"X-Nexus-Signature": h["X-Nexus-Signature"]}, b),  # нет timestamp
    lambda h, b: ({**h, "X-Nexus-Timestamp": "abc"}, b),
    lambda h, b: ({**h, "X-Nexus-Signature": h["X-Nexus-Signature"].replace("v1=", "v2=")}, b),
])
def test_invalid_signatures_rejected(mutate):
    secret = _reference_secret("k-123")
    headers, body = mutate(_signed(BODY, secret), BODY)
    with pytest.raises(WebhookVerificationError):
        webhooks.unwrap(body, headers, secret)


def test_old_timestamp_rejected_unless_tolerance_disabled():
    secret = _reference_secret("k-123")
    old = _signed(BODY, secret, ts=int(time.time()) - 3600)
    with pytest.raises(WebhookVerificationError, match="tolerance"):
        webhooks.verify_signature(BODY, old, secret)
    webhooks.verify_signature(BODY, old, secret, tolerance=0)


def test_non_json_body_rejected():
    secret = _reference_secret("k-123")
    with pytest.raises(WebhookVerificationError, match="JSON"):
        webhooks.unwrap(b"not json", _signed(b"not json", secret), secret)


def test_client_webhooks_unwrap_uses_key_secret_and_fetches_secret():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/webhooks/secret"
        return httpx.Response(200, json={"secret": _reference_secret("k-123"), "algorithm": "HMAC-SHA256"})

    client = _client(handler)
    assert client.webhooks.secret() == _reference_secret("k-123")
    event = client.webhooks.unwrap(BODY, _signed(BODY, _reference_secret("k-123")))
    assert event["task_id"] == "t-1"
    with pytest.raises(WebhookVerificationError):
        client.webhooks.unwrap(BODY, _signed(BODY, _reference_secret("other-key")))


@pytest.mark.asyncio
async def test_async_webhooks():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"secret": "whsec_x"})

    async with AsyncNexusClient(api_key="k-123", base_url="https://api.test",
                                transport=httpx.MockTransport(handler)) as client:
        assert await client.webhooks.secret() == "whsec_x"
        assert client.webhooks.unwrap(BODY, _signed(BODY, _reference_secret("k-123")))["status"] == "completed"


# ============================================================
# Задачи и типы
# ============================================================


def test_task_pickles_without_client():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"task_id": "t", "status": "completed", "result": {"image_urls": ["u"]}})

    task = _client(handler).tasks.get("t")
    restored = pickle.loads(pickle.dumps(task))
    assert restored.id == "t" and restored.image_url == "u"
    assert restored._client is None
    with pytest.raises(NexusError, match="not bound"):
        restored.refresh()


def test_typed_responses_are_plain_dicts():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"object": "list", "data": [{"id": "m", "object": "model"}]})

    models = _client(handler).v1.models.list()
    assert isinstance(models[0], dict) and models[0]["id"] == "m"
    assert hasattr(nexusapi.types, "ChatCompletion") and nexusapi.__version__ == "0.4.0"


# ============================================================
# Исправления по ревью 0.4.0
# ============================================================


def test_empty_api_key_is_not_replaced_by_env(monkeypatch):
    monkeypatch.setenv("NEXUSAPI_API_KEY", "operator-key")
    with pytest.raises(AuthenticationError):
        NexusClient(api_key="")
    assert NexusClient(api_key=None)._api_key == "operator-key"


def test_headers_are_case_insensitive_and_sdk_wins():
    seen: list[list[tuple[str, str]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(list(request.headers.multi_items()))
        return httpx.Response(202, json={"task_id": "t"})

    client = _client(handler, default_headers={
        "authorization": "Bearer other", "user-agent": "custom/1", "idempotency-key": "static-key-1",
    })
    client.videos.create(model="m", prompt="p")
    names = [k.lower() for k, _ in seen[0]]
    assert names.count("authorization") == 1 and names.count("idempotency-key") == 1
    headers = dict(seen[0])
    assert headers["authorization"] == "Bearer k-123"
    assert headers["idempotency-key"].startswith("sdk-")
    assert headers["user-agent"] == "custom/1"  # свой User-Agent можно


def test_explicit_timeout_bounds_the_whole_call(monkeypatch):
    import nexusapi.client as cm

    clock = {"t": 0.0}
    monkeypatch.setattr(cm.time, "monotonic", lambda: clock["t"])
    monkeypatch.setattr(cm.time, "sleep", lambda s: clock.__setitem__("t", clock["t"] + s))
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        clock["t"] += 30  # каждая попытка «длится» до таймаута
        raise httpx.ReadTimeout("slow")

    with pytest.raises(APITimeoutError):
        _client(handler, max_retries=5).chat.completions.create(model="m", messages=[], timeout=30)
    assert calls["n"] == 1  # после 30 с времени на повтор нет


def test_chat_retries_stop_after_lock_window(monkeypatch):
    import nexusapi.client as cm

    clock = {"t": 0.0}
    monkeypatch.setattr(cm.time, "monotonic", lambda: clock["t"])
    monkeypatch.setattr(cm.time, "sleep", lambda s: clock.__setitem__("t", clock["t"] + s))
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        clock["t"] += 600  # nginx отдал 504 через 600 с
        return httpx.Response(504, json={"error": {"message": "gateway timeout"}})

    with pytest.raises(InternalServerError):
        _client(handler, max_retries=3).chat.completions.create(model="m", messages=[])
    assert calls["n"] == 1  # после 540 с повтор мог бы запустить модель второй раз


def test_images_do_not_retry_failed_task_500(no_sleep):
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(500, json={"error": {"message": "Task failed"}})

    with pytest.raises(InternalServerError):
        _client(handler, max_retries=3).v1.images.generate(model="m", prompt="p")
    assert calls["n"] == 1


@pytest.fixture
def no_sleep(monkeypatch):
    import nexusapi.client as cm

    monkeypatch.setattr(cm.time, "sleep", lambda _: None)


def test_non_ascii_signature_is_rejected_cleanly():
    secret = _reference_secret("k-123")
    headers = _signed(BODY, secret)
    headers["X-Nexus-Signature"] = "v1=\xe9\xe9"
    with pytest.raises(WebhookVerificationError):
        webhooks.verify_signature(BODY, headers, secret)


def test_copy_keeps_client_pickle_drops_it():
    import copy as copy_module

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"task_id": "t", "status": "completed", "result": {"x": [1]}})

    task = _client(handler).tasks.get("t")
    shallow = copy_module.copy(task)
    deep = copy_module.deepcopy(task)
    assert shallow.refresh().id == "t" and deep.refresh().id == "t"
    assert deep.result is not task.result
    assert pickle.loads(pickle.dumps(task))._client is None


def test_stream_timeout_is_api_timeout_error():
    class Slow(httpx.SyncByteStream):
        def __iter__(self):
            yield b'data: {"choices": []}\n\n'
            raise httpx.ReadTimeout("idle")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=Slow())

    stream = _client(handler).chat.completions.create(model="m", messages=[], stream=True)
    next(stream)
    with pytest.raises(APITimeoutError):
        next(stream)
