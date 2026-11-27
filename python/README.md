# NexusAPI Python SDK

Официальный Python SDK для [NexusAPI](https://docs.nexusapi.dev) — единый API для
генерации видео и изображений (Veo, Kling, Seedance, Nano-Banana и др.) и
текстовых моделей (Claude, GPT) через OpenAI-совместимый `/v1`.

## Установка

```bash
pip install nexusapi-sdk
```

Требуется Python 3.10+.

> Пакет на PyPI называется `nexusapi-sdk` (имя `nexusapi` зарезервировано
> похожим проектом). Импорт остаётся `from nexusapi import ...`.

Ключ API создаётся в [panel.nexusapi.dev](https://panel.nexusapi.dev). Удобнее
всего положить его в переменную окружения — тогда в коде его не будет:

```bash
export NEXUSAPI_API_KEY="..."
```

## Quickstart — sync

```python
from nexusapi import NexusClient

with NexusClient() as client:  # ключ из NEXUSAPI_API_KEY (или NexusClient(api_key=...))
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
    async with AsyncNexusClient() as client:
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

## Настройки клиента

```python
import httpx
from nexusapi import NexusClient

client = NexusClient(
    api_key="...",                       # по умолчанию NEXUSAPI_API_KEY
    base_url="https://nexusapi.dev",     # по умолчанию NEXUSAPI_BASE_URL
    timeout=60,                          # секунды или httpx.Timeout
    max_retries=2,                       # повторы при 429/5xx/сетевых ошибках
    default_headers={"X-Team": "media"}, # заголовки каждого запроса
    http_client=httpx.Client(proxy="http://proxy:3128"),  # прокси, TLS, лимиты (proxy= — httpx 0.26+)
)

# Разовые настройки — копия клиента на том же пуле соединений
client.with_options(timeout=10, max_retries=0).tasks.get(task_id)
```

Свой `http_client` SDK не закрывает — это делает тот, кто его создал. Таймаут
и повторы при этом берутся из настроек SDK.

Таймаут клиента (`timeout=`) действует на одну попытку. Явный `timeout=` у
`chat.completions.create` и `v1.images.generate` — общий лимит на весь вызов
вместе с повторами. Без него `v1.images.generate` при повторах 504 может ждать
дольше 180 с (до ~9 минут, у Seedream Pro — дольше); ограничьте его
`timeout=` или `max_retries`.

## Вебхуки и проверка подписи

Передайте `webhook_url` при создании задачи — когда она завершится, NexusAPI
пришлёт POST с телом `{"task_id", "status", "result", "error"}` и подписью в
заголовках `X-Nexus-Timestamp` / `X-Nexus-Signature`. Проверяйте подпись по
**сырому** телу запроса:

```python
from fastapi import FastAPI, HTTPException, Request
from nexusapi import NexusClient, WebhookVerificationError

app = FastAPI()
client = NexusClient()

@app.post("/nexus-callback")
async def nexus_callback(request: Request):
    body = await request.body()  # сырые байты, не request.json()
    try:
        event = client.webhooks.unwrap(body, request.headers)
    except WebhookVerificationError:
        raise HTTPException(400, "bad signature")
    if event["status"] == "completed":
        save_result(event["task_id"], event["result"])
    return {"ok": True}
```

`client.webhooks.unwrap` вычисляет секрет из ключа клиента. Если сервис,
принимающий вебхуки, не должен знать API-ключ, получите секрет один раз
(`client.webhooks.secret()` → `whsec_…`) и проверяйте им:

```python
from nexusapi import webhooks

event = webhooks.unwrap(body, request.headers, secret=WEBHOOK_SECRET)
```

Подпись старше 5 минут отклоняется (`tolerance=` меняет допуск) — так
перехваченный запрос нельзя прислать повторно.

## Возможности

- **Sync (`NexusClient`) и async (`AsyncNexusClient`)** — одинаковый API,
  можно использовать в скриптах или внутри FastAPI/aiohttp без блокировки
  event loop.
- **Auto-retries** — на 5xx, 429 и сетевые ошибки. По умолчанию до 3 попыток
  с экспоненциальным backoff (0.5s, 1s, 2s + jitter). Учитывает `Retry-After`
  (секунды, HTTP-дата, `retry-after-ms`; больше 60 с не ждёт). Настраивается
  через `max_retries=N`. Повторы пишутся в логгер `nexusapi` на уровне INFO.
- **Auto Idempotency-Key** — для каждого POST автоматически генерируется
  UUID v4, если не передан свой (8–256 символов). Генерации (`/generate`,
  `/v1/images`, `/v1/videos`) сервер привязывает к ключу: повтор с тем же
  ключом — после 5xx, таймаута или обрыва — возвращает ту же задачу и не
  списывает деньги второй раз. Если повтор получил 409 «первый запрос ещё
  выполняется», SDK ждёт до 2 минут его ответ.
- **Что делать после ошибки.** У ошибок POST-запросов есть `idempotency_key`,
  у ошибок с ответом сервера — `request_id`.
  - Генерации: повторяйте с тем же `idempotency_key`, а не с новым — сервер
    вернёт ту же задачу. 504 у `v1.images.generate` несёт `task_id`: задачу
    можно дождаться через `client.tasks.get(err.task_id).wait()`.
  - Чат: сервер хранит только успешный ответ — после 5xx повтор вызовет
    модель снова. Поэтому 500 и 503 SDK у чата сам не повторяет, а остальные
    повторы делает только в первые 9 минут вызова, пока сервер держит замок
    ключа.
  - Стрим чата повторить нельзя: сервер его не сохраняет.
  - В поддержку пишите с `request_id` — по нему находят запрос в логах.
- **Долгие запросы** — `POST /generate` ждёт ответа не меньше 120 с (часть
  моделей до ответа проверяет входные файлы), `chat.completions` и
  `v1.images.generate` — не меньше 600 с (аргумент `timeout=`).
- **Ожидание задач** — `task.wait()` и `client.v1.videos.wait()` пережидают
  временные ошибки опроса (сеть, 429, 5xx). Если время вышло, задача
  продолжает идти: `NexusTimeoutError` несёт `.task_id` и `.task` — дождитесь
  её ещё раз, а не создавайте новую. `images.generate` по умолчанию ждёт
  до 600 с.
- **Все native-параметры** через `**kwargs` — `webhook_url`, `negative_prompt`,
  `image_url`, `cfg_scale`, `video_urls` для seedance, `source_video_url` для veo-extend,
  и т.д. Передаются прямо в `create()`/`generate()`.
- **Auto-polling** для image-моделей (sync UX). Для видео — `task.wait()`.
- **Типизированные ошибки** — все наследуют `NexusError`:
  - `APIStatusError` — любой HTTP-ответ с ошибкой; подклассы
    `ValidationError` (400/422), `AuthenticationError` (401), `BillingError`
    (402), `NexusPermissionError` (403), `NotFoundError` (404), `ConflictError`
    (409), `RateLimitError` (429), `InternalServerError` (5xx);
  - `NetworkError` (ответа нет) и его подкласс `APITimeoutError`;
  - `APIResponseValidationError` (ответ не JSON или без нужного поля),
    `StreamError` (ошибка внутри стрима), `NexusTimeoutError` (не дождались
    задачи), `TaskFailedError` (задача упала), `WebhookVerificationError`.

  Поля: `status_code`, `headers`, `response_body`, `request_id`, `code`,
  `type`, `param`, `idempotency_key`, у ошибок про задачу — `task_id`.
- **Типы** — ответы описаны в `nexusapi.types` (TypedDict: `ChatCompletion`,
  `Video`, `ImagesResponse`, …); пакет проходит `mypy --strict`. Во время
  выполнения это обычные dict.
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
# Не вызываем .wait() — NexusAPI пришлёт подписанный POST, когда видео готово
return task.id  # сохраним в БД, дождёмся колбэка
```

Как проверить подпись вебхука — в разделе «Вебхуки и проверка подписи».

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
from nexusapi import APIStatusError, BillingError, NetworkError, RateLimitError, ValidationError

try:
    task = client.videos.create(model="veo-3-fast", prompt="...", duration=8).wait()
except BillingError:
    print("Недостаточно средств — пополните баланс")
except RateLimitError:
    print("Превышен rate-limit ключа — подождите минуту")
except ValidationError as e:
    print(f"Невалидные параметры: {e}")
except APIStatusError as e:
    print(f"Ошибка API {e.status_code}, request_id={e.request_id}")
except NetworkError as e:
    # повторять — с тем же ключом: сервер вернёт ту же задачу
    print(f"Нет ответа; повторите с idempotency_key={e.idempotency_key}")
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
