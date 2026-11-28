"""Смоук-тест Python SDK против живого API.

    export NEXUSAPI_API_KEY=...
    python smoke/python_smoke.py            # бесплатные проверки
    python smoke/python_smoke.py --paid     # + платные, около 1 ₽

Берёт SDK из ./python (текущий код репозитория), а не из установленного пакета.
Ключ не печатает. Код выхода 0 — все проверки прошли.
"""
from __future__ import annotations

import argparse
import os
import pathlib
import sys
import time
import uuid
from typing import Any, Callable

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "python"))

from nexusapi import (  # noqa: E402
    NexusClient,
    NotFoundError,
    __version__,
    webhooks,
)

CHAT_MODEL = "gpt-6-luna"     # самая дешёвая текстовая модель
IMAGE_MODEL = "nano-banana"   # самая дешёвая картинка (0,8 ₽)

results: list[bool] = []


def check(name: str, fn: Callable[[], Any]) -> None:
    started = time.monotonic()
    try:
        detail = fn()
    except Exception as e:  # noqa: BLE001 — смоук-тест: печатаем любую ошибку
        rid = getattr(e, "request_id", None)
        print(f"✗ {name}: {type(e).__name__}: {e}" + (f" (request_id={rid})" if rid else ""))
        results.append(False)
        return
    print(f"✓ {name} ({time.monotonic() - started:.1f}s): {detail}")
    results.append(True)


def expect_not_found(fn: Callable[[], Any]) -> str:
    try:
        fn()
    except NotFoundError as e:
        if not e.request_id:
            raise AssertionError("404 without X-Request-Id") from e
        return f"404, request_id={e.request_id}"
    raise AssertionError("expected NotFoundError")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--paid", action="store_true", help="also run paid checks (~1 RUB)")
    args = parser.parse_args()
    api_key = os.environ.get("NEXUSAPI_API_KEY")
    if not api_key:
        print("set NEXUSAPI_API_KEY")
        return 2

    print(f"nexusapi-sdk {__version__} (python) → {os.environ.get('NEXUSAPI_BASE_URL') or 'https://nexusapi.dev'}")
    with NexusClient() as client:
        check("public catalog", lambda: f"{len(client.models.list())} models")
        check("auth + /v1/models", lambda: f"{len(client.v1.models.list())} models for this key")

        def secret() -> str:
            value = client.webhooks.secret()
            if value != webhooks.secret_from_api_key(api_key):
                raise AssertionError("server secret differs from SDK derivation")
            return "matches SDK derivation"

        check("webhook secret", secret)
        check("unknown task → 404", lambda: expect_not_found(lambda: client.tasks.get(f"smoke-{uuid.uuid4().hex}")))
        check("unknown chat model → 404",
              lambda: expect_not_found(lambda: client.chat.completions.create(model="no-such-model", messages=[])))

        if args.paid:
            prompt = [{"role": "user", "content": "Reply with exactly one word: pong"}]

            def chat() -> str:
                reply = client.chat.completions.create(model=CHAT_MODEL, messages=prompt, max_tokens=200)
                text = reply["choices"][0]["message"].get("content") or ""
                if not text.strip():
                    raise AssertionError(f"empty content: {reply}")
                return f"{text.strip()[:40]!r}, usage={reply.get('usage')}"

            def chat_stream() -> str:
                stream = client.chat.completions.create(model=CHAT_MODEL, messages=prompt, max_tokens=200, stream=True)
                final = stream.final_completion()
                text = final["choices"][0]["message"].get("content") or ""
                if not text.strip():
                    raise AssertionError(f"empty stream: {final}")
                return f"{text.strip()[:40]!r}, finish={final['choices'][0].get('finish_reason')}"

            def image_idempotent() -> str:
                key = f"smoke-{uuid.uuid4().hex}"
                first = client.images.generate(model=IMAGE_MODEL, prompt="A small red cube", wait=False, idempotency_key=key)
                second = client.images.generate(model=IMAGE_MODEL, prompt="A small red cube", wait=False, idempotency_key=key)
                if first.id != second.id:
                    raise AssertionError(f"same key gave two tasks: {first.id} vs {second.id}")
                first.wait(timeout=300, poll_interval=2)
                if not first.image_url:
                    raise AssertionError(f"no image url: {first.result}")
                return f"one task {first.id} for two requests, {first.image_url[:60]}…"

            check(f"chat {CHAT_MODEL}", chat)
            check(f"chat stream {CHAT_MODEL}", chat_stream)
            check(f"image {IMAGE_MODEL} + idempotent replay", image_idempotent)

    failed = results.count(False)
    print(f"\n{len(results) - failed}/{len(results)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
