"""NexusAPI Python SDK.

Sync (NexusClient) и async (AsyncNexusClient) клиенты с одинаковым публичным
API. Auto-retries на 5xx/429/network errors, auto Idempotency-Key для POST.

Quickstart — sync:

    from nexusapi import NexusClient

    with NexusClient() as client:  # ключ из NEXUSAPI_API_KEY
        # Image (built-in polling)
        img = client.images.generate(model="nano-banana", prompt="a cat")
        print(img.image_url)

        # Video (получаем Task, ждём отдельно)
        task = client.videos.create(model="veo-3-fast", prompt="a cat", duration=8)
        task.wait()
        print(task.video_url)

        # Текст (OpenAI-совместимый /v1/chat/completions)
        reply = client.chat.completions.create(
            model="claude-sonnet-5",
            messages=[{"role": "user", "content": "Привет"}],
        )
        print(reply["choices"][0]["message"]["content"])

        # Стрим
        with client.chat.completions.create(
            model="claude-sonnet-5", messages=[...], stream=True,
        ) as stream:
            for chunk in stream:
                if chunk["choices"]:
                    print(chunk["choices"][0]["delta"].get("content") or "", end="")

Quickstart — async:

    import asyncio
    from nexusapi import AsyncNexusClient

    async def main():
        async with AsyncNexusClient(api_key="...") as client:
            task = await client.videos.create(
                model="veo-3-fast", prompt="a cat", duration=8,
            )
            await task.wait()
            print(task.video_url)

    asyncio.run(main())
"""
from . import types, webhooks
from .client import (
    NOT_GIVEN,
    AsyncNexusClient,
    AsyncStream,
    AsyncTask,
    NexusClient,
    NotGiven,
    Stream,
    Task,
)
from .errors import (
    APIResponseValidationError,
    APIStatusError,
    APITimeoutError,
    AuthenticationError,
    BillingError,
    ConflictError,
    InternalServerError,
    NetworkError,
    NexusError,
    NotFoundError,
    PermissionError as NexusPermissionError,
    RateLimitError,
    StreamError,
    TaskFailedError,
    TimeoutError as NexusTimeoutError,
    ValidationError,
    WebhookVerificationError,
)

__version__ = "0.4.0"

__all__ = [
    # Clients
    "NexusClient",
    "AsyncNexusClient",
    "NOT_GIVEN",
    "NotGiven",
    # Task objects
    "Task",
    "AsyncTask",
    # Chat streams
    "Stream",
    "AsyncStream",
    # Modules
    "types",
    "webhooks",
    # Errors
    "NexusError",
    "APIStatusError",
    "AuthenticationError",
    "BillingError",
    "NexusPermissionError",
    "RateLimitError",
    "ValidationError",
    "NotFoundError",
    "ConflictError",
    "InternalServerError",
    "NetworkError",
    "APITimeoutError",
    "StreamError",
    "APIResponseValidationError",
    "NexusTimeoutError",
    "TaskFailedError",
    "WebhookVerificationError",
]
