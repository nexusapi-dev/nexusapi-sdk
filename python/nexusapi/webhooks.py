"""Проверка подписи вебхуков NexusAPI.

Каждый вебхук приходит с заголовками::

    X-Nexus-Timestamp: 1764288000
    X-Nexus-Signature: v1=<hex HMAC-SHA256(secret, "{timestamp}.{сырое тело}")>

Секрет — `whsec_…`: его отдаёт `client.webhooks.secret()` (GET /webhooks/secret),
и он же вычисляется из API-ключа, которым создана задача. По секрету нельзя
восстановить ключ, поэтому сервису, который только принимает вебхуки, достаточно
секрета.

Пример (FastAPI)::

    from nexusapi import webhooks

    @app.post("/nexus-callback")
    async def callback(request: Request):
        body = await request.body()  # именно сырые байты, не request.json()
        try:
            event = webhooks.unwrap(body, request.headers, secret=WEBHOOK_SECRET)
        except webhooks.WebhookVerificationError:
            raise HTTPException(400)
        ...

Подпись и timestamp защищают от поддельных и повторно отправленных запросов;
результат задачи всё равно можно перепроверить через `client.tasks.get`.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time
from typing import Any, Mapping, Optional, Union, cast

from .errors import WebhookVerificationError
from .types import WebhookEvent

SIGNATURE_HEADER = "X-Nexus-Signature"
TIMESTAMP_HEADER = "X-Nexus-Timestamp"
SIGNATURE_VERSION = "v1"
SECRET_PREFIX = "whsec_"
#: Допустимое расхождение часов отправителя и получателя, с.
DEFAULT_TOLERANCE = 300

_DERIVATION_LABEL = b"nexus-webhook-signing-v1"

Payload = Union[bytes, bytearray, str]


def secret_from_api_key(api_key: str) -> str:
    """Секрет подписи вебхуков для задач этого API-ключа (без запроса к API).

    Тот же, что возвращает GET /webhooks/secret.
    """
    if not api_key:
        raise ValueError("api_key is required")
    digest = hmac.new(api_key.encode("utf-8"), _DERIVATION_LABEL, hashlib.sha256).hexdigest()
    return SECRET_PREFIX + digest


def _header(headers: Mapping[str, Any], name: str) -> Optional[str]:
    """Значение заголовка без учёта регистра (dict, Starlette, Werkzeug, httpx…)."""
    value = headers.get(name)
    if value is None:
        lower = name.lower()
        for key, item in headers.items():
            if isinstance(key, str) and key.lower() == lower:
                value = item
                break
    if isinstance(value, (list, tuple)):
        value = ",".join(str(v) for v in value)
    return None if value is None else str(value)


def _payload_bytes(payload: Payload) -> bytes:
    if isinstance(payload, str):
        return payload.encode("utf-8")
    return bytes(payload)


def verify_signature(
    payload: Payload,
    headers: Mapping[str, Any],
    secret: str,
    *,
    tolerance: float = DEFAULT_TOLERANCE,
    now: Optional[float] = None,
) -> None:
    """Проверяет подпись вебхука. Ничего не возвращает, при ошибке бросает
    WebhookVerificationError.

    Args:
        payload: сырое тело запроса (bytes) — именно то, что пришло, без
            повторной сериализации JSON.
        headers: заголовки запроса (любой mapping, регистр не важен).
        secret: секрет `whsec_…`.
        tolerance: на сколько секунд timestamp может отличаться от текущего
            времени. 0 или меньше — не проверять время (не рекомендуется).
    """
    if not secret:
        raise WebhookVerificationError("Webhook secret is empty")
    timestamp_raw = _header(headers, TIMESTAMP_HEADER)
    signature_raw = _header(headers, SIGNATURE_HEADER)
    if not timestamp_raw or not signature_raw:
        raise WebhookVerificationError(
            f"Missing {TIMESTAMP_HEADER} or {SIGNATURE_HEADER} header"
        )
    try:
        timestamp = int(timestamp_raw.strip())
    except ValueError as e:
        raise WebhookVerificationError(f"Invalid {TIMESTAMP_HEADER} header") from e
    if tolerance > 0:
        current = time.time() if now is None else now
        if abs(current - timestamp) > tolerance:
            raise WebhookVerificationError(
                f"Webhook timestamp is outside the tolerance of {tolerance:g}s"
            )
    signed = str(timestamp).encode("ascii") + b"." + _payload_bytes(payload)
    expected = hmac.new(secret.encode("utf-8"), signed, hashlib.sha256).hexdigest()
    for part in signature_raw.split(","):
        version, _, value = part.strip().partition("=")
        if version == SIGNATURE_VERSION and hmac.compare_digest(value.strip(), expected):
            return
    raise WebhookVerificationError("Webhook signature does not match")


def unwrap(
    payload: Payload,
    headers: Mapping[str, Any],
    secret: str,
    *,
    tolerance: float = DEFAULT_TOLERANCE,
) -> WebhookEvent:
    """Проверяет подпись и возвращает событие (`task_id`, `status`, `result`, `error`).

    Raises:
        WebhookVerificationError: подпись не прошла проверку или тело не JSON-объект.
    """
    verify_signature(payload, headers, secret, tolerance=tolerance)
    try:
        event = json.loads(_payload_bytes(payload))
    except ValueError as e:
        raise WebhookVerificationError("Webhook body is not valid JSON") from e
    if not isinstance(event, dict):
        raise WebhookVerificationError("Webhook body is not a JSON object")
    return cast(WebhookEvent, event)


__all__ = [
    "DEFAULT_TOLERANCE",
    "SIGNATURE_HEADER",
    "TIMESTAMP_HEADER",
    "WebhookVerificationError",
    "secret_from_api_key",
    "unwrap",
    "verify_signature",
]
