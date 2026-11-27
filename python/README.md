# NexusAPI Python SDK

Официальный Python SDK для [NexusAPI](https://nexusapi.dev) — единый API для
генерации видео и изображений (Veo, Kling, Seedance, Nano-Banana и др.) и
текстовых моделей (Claude, GPT) через OpenAI-совместимый `/v1`.

## Установка

```bash
pip install nexusapi-sdk
```

> Пакет на PyPI называется `nexusapi-sdk` (имя `nexusapi` зарезервировано
> похожим проектом). Импорт остаётся `from nexusapi import ...`.

## Quickstart — sync

```python
from nexusapi import NexusClient

with NexusClient(api_key="ВАШ_КЛЮЧ") as client:  # создать в panel.nexusapi.dev
    # Изображение (sync — SDK сам опрашивает статус)
    img = client.images.generate(
        model="nano-banana",
        prompt="Минималистичный логотип кофейни",
        aspect_ratio="1:1",
    )
    print(img.image_url)
    # → https://nexusapi-s3.../result.png

    # Видео — получаем Task, ждём отдельно (до 10 минут на генерацию)
    task = client.videos.create(
        model="veo-3-fast",
        prompt="Кот играет на пианино",
        duration=8,
        aspect_ratio="16:9",
    )
    task.wait(timeout=600)
    print(task.video_url)
```

## Quickstart — async (FastAPI / aiohttp / asyncio)

```python
import asyncio
from nexusapi import AsyncNexusClient

async def main():
    async with AsyncNexusClient(api_key="ВАШ_КЛЮЧ") as client:
        task = await client.videos.create(
            model="veo-3-fast",
            prompt="Кот играет на пианино",
            duration=8,
        )
        await task.wait()
        print(task.video_url)

asyncio.run(main())
```

Публичный API идентичен sync — все методы те же, отличается только `await`.

## Текстовые модели — `chat.completions`

`POST /v1/chat/completions` в формате OpenAI. Ответ — `dict` в той же форме,
что у OpenAI API.

```python
reply = client.chat.completions.create(
    model="claude-sonnet-5",
    messages=[
        {"role": "system", "content": "Отвечай коротко."},
        {"role": "user", "content": "Что такое идемпотентность?"},
    ],
    max_tokens=500,
)
print(reply["choices"][0]["message"]["content"])
print(reply["usage"])  # prompt_tokens, completion_tokens, ...
```

Любые поля OpenAI API (`temperature`, `tools`, `tool_choice`,
`response_format`, `reasoning_effort`, ...) передаются как именованные
аргументы. Список моделей — `client.v1.models.list()` или
[docs.nexusapi.dev/models/llm](https://docs.nexusapi.dev/models/llm/).

### Стриминг (SSE)

```python
with client.chat.completions.create(
    model="claude-sonnet-5",
    messages=[{"role": "user", "content": "Расскажи историю"}],
    stream=True,
) as stream:
    for chunk in stream:
        if chunk["choices"]:
            print(chunk["choices"][0]["delta"].get("content") or "", end="", flush=True)
```

Async: `stream = await client.chat.completions.create(..., stream=True)`, затем
`async for chunk in stream`. Если провайдер не смог закончить ответ, сервер
присылает ошибку внутри стрима — SDK бросает `StreamError`; обрыв соединения
посреди стрима — `NetworkError`.

### Vision

```python
reply = client.chat.completions.create(
    model="gpt-6-luna",
    messages=[{"role": "user", "content": [
        {"type": "text", "text": "Что на изображении?"},
        {"type": "image_url", "image_url": {"url": "https://example.com/image.jpg"}},
    ]}],
)
```

## OpenAI-совместимые картинки и видео — `client.v1`

Нативные `client.images` / `client.videos` работают через `POST /generate`.
Те же модели доступны в форме OpenAI / Sora через `client.v1`:

```python
# Модели, доступные ключу (GET /v1/models, нужен ключ)
ids = [m["id"] for m in client.v1.models.list()]

# Картинка: сервер сам ждёт результат (до 180 с, у Seedream Pro до 540 с)
img = client.v1.images.generate(model="nano-banana", prompt="Кот", size="1024x1024")
print(img["data"][0]["url"])

# Видео: создание сразу возвращает объект, потом ждём
video = client.v1.videos.create(model="veo-3-fast", prompt="Кот", seconds=8, size="1920x1080")
video = client.v1.videos.wait(video, timeout=600)   # или retrieve(video["id"])
print(video["url"])
```

`client.v1.chat` — тот же объект, что и `client.chat`.

## Возможности

- **Sync (`NexusClient`) и async (`AsyncNexusClient`)** — одинаковый API,
  можно использовать в скриптах или внутри FastAPI/aiohttp без блокировки
  event loop.
- **Auto-retries** — на 5xx, 429 и сетевые ошибки. По умолчанию до 3 попыток
  с экспоненциальным backoff (0.5s, 1s, 2s + jitter). Учитывает `Retry-After`
  header. Настраивается через `max_retries=N`.
- **Auto Idempotency-Key** — для каждого POST автоматически генерируется
  UUID v4 если не передан свой. Retry безопасен — двойного списания не будет.
  Для стрима чата ключ уходит только если передан явно, а повтор делается
  только когда запрос не дошёл до сервера.
- **Долгие запросы** — для `chat.completions` и `v1.images.generate` таймаут
  чтения не меньше 600 с (настраивается аргументом `timeout=`). Ответы 500,
  502 и 504 этих запросов не повторяются: задача на сервере могла уже
  начаться, и повтор оплатил бы её второй раз. Если соединение оборвалось
  после отправки, SDK тоже не повторяет запрос сам, а бросает `NetworkError`
  с `idempotency_key` — повторите вызов с этим ключом и получите результат
  первой генерации без второго списания (пока она идёт — `ConflictError`).
- **Все native-параметры** через `**kwargs` — `webhook_url`, `negative_prompt`,
  `image_url`, `cfg_scale`, `video_urls` для seedance, `source_video_url` для veo-extend,
  и т.д. Передаются прямо в `create()`/`generate()`.
- **Auto-polling** для image-моделей (sync UX). Для видео — `task.wait()`.
- **Типизированные ошибки** — `NexusError` (база), `BillingError`,
  `RateLimitError`, `ValidationError`, `NotFoundError`, `AuthenticationError`,
  `NexusPermissionError`, `NexusTimeoutError` (client-side wait timeout),
  `TaskFailedError` (провайдер не справился), `NetworkError` (retries исчерпаны),
  `ConflictError` (409: запрос с этим Idempotency-Key ещё выполняется),
  `StreamError` (ошибка внутри SSE-стрима).
- **Context manager** — `with NexusClient(...) as c:` / `async with AsyncNexusClient(...) as c:`
  закрывает HTTP-соединение автоматически.

## Примеры

### Webhook вместо polling

```python
task = client.videos.create(
    model="veo-3-fast",
    prompt="...",
    duration=8,
    webhook_url="https://your-app.com/nexus-callback",
)
# Не вызываем .wait() — NexusAPI POST'нет на webhook_url когда видео готово
return task.id  # сохраним в БД, дождёмся колбэка
```

### Image-to-video (Kling motion)

```python
video = client.videos.create(
    model="kling-v2.6-motion-1080p",
    prompt="Камера медленно отъезжает, раскрывая пейзаж",
    duration=8,
    image_url="https://your-bucket.s3.../start-frame.jpg",
).wait(timeout=600)
print(video.video_url)
```

### Image edit (nano-banana)

```python
edited = client.images.generate(
    model="nano-banana-pro",
    prompt="Сделай небо более драматичным",
    image_url="https://your-bucket.s3.../photo.jpg",
)
print(edited.image_url)
```

### Извлечение результата

```python
task = client.videos.create(...).wait()
print(task.video_url)        # для видео-моделей
print(task.image_url)        # для image-моделей (берёт первое из image_urls)
print(task.image_urls)       # для image-моделей с несколькими изображениями
print(task.result)           # сырой dict с провайдер-специфичным контентом
```

### Обработка ошибок

```python
from nexusapi import NexusClient, BillingError, RateLimitError, ValidationError

try:
    task = client.videos.create(model="veo-3-fast", prompt="...", duration=8).wait()
except BillingError:
    print("Недостаточно средств — пополни баланс в боте")
except RateLimitError:
    print("Превышен rate-limit ключа — подожди минуту")
except ValidationError as e:
    print(f"Невалидные параметры: {e}")
```

### Каталог моделей

```python
models = client.models.list()
for m in models:
    print(f"{m['id']}: {m['priceRub']}₽ за {m['unit']}")
```

## Документация

- [docs.nexusapi.dev](https://docs.nexusapi.dev) — гайды и каталог моделей
- [docs.nexusapi.dev/api-reference](https://docs.nexusapi.dev/api-reference) — полный API reference
- [docs.nexusapi.dev/models](https://docs.nexusapi.dev/models) — параметры каждой модели

## Лицензия

MIT
