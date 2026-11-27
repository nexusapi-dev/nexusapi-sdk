# NexusAPI Node.js SDK

Официальный Node.js / TypeScript SDK для [NexusAPI](https://docs.nexusapi.dev) —
единый API для генерации видео и изображений (Veo, Kling, Seedance,
Nano-Banana и др.) и текстовых моделей (Claude, GPT) через OpenAI-совместимый
`/v1`.

## Установка

```bash
npm install nexusapi-sdk
```

Требует **Node.js 20+** (используем встроенные `fetch` и WebCrypto). Без внешних
зависимостей. Работает и через `import`, и через `require` (сборки ESM и
CommonJS), а также в Deno, Bun и edge-средах.

Ключ API создаётся в [panel.nexusapi.dev](https://panel.nexusapi.dev). Удобнее
всего положить его в переменную окружения `NEXUSAPI_API_KEY` — тогда в коде
его не будет.

> Пакет на npm называется `nexusapi-sdk` (имя `nexusapi` зарезервировано
> другим проектом). Mirror Python SDK: `pip install nexusapi-sdk`.

## Quickstart

```ts
import { NexusClient } from "nexusapi-sdk";

const client = new NexusClient(); // ключ из NEXUSAPI_API_KEY (или { apiKey: "..." })

// Изображение (sync — SDK сам опрашивает статус)
const img = await client.images.generate({
  model: "nano-banana",
  prompt: "Минималистичный логотип кофейни",
  aspect_ratio: "1:1",
});
console.log(img.imageUrl);
// → https://nexusapi-s3.../result.png

// Видео (async — получаем Task, ждём отдельно)
const task = await client.videos.create({
  model: "veo-3-fast",
  prompt: "Кот играет на пианино",
  duration: 8,
  aspect_ratio: "16:9",
});
console.log(`Task ${task.id}, status=${task.status}`);

// Polling до завершения (до 10 минут)
await task.wait();
console.log(task.videoUrl);
```

## Текстовые модели — `chat.completions`

`POST /v1/chat/completions` в формате OpenAI.

```ts
const reply = await client.chat.completions.create({
  model: "claude-sonnet-5",
  messages: [
    { role: "system", content: "Отвечай коротко." },
    { role: "user", content: "Что такое идемпотентность?" },
  ],
  max_tokens: 500,
});
console.log(reply.choices[0].message.content);
console.log(reply.usage); // prompt_tokens, completion_tokens, ...
```

Любые поля OpenAI API (`temperature`, `tools`, `tool_choice`,
`response_format`, `reasoning_effort`, ...) передаются в том же объекте.
Список моделей — `client.v1.models.list()` или
[docs.nexusapi.dev/models/llm](https://docs.nexusapi.dev/models/llm/).

### Стриминг (SSE)

```ts
const stream = await client.chat.completions.create({
  model: "claude-sonnet-5",
  messages: [{ role: "user", content: "Расскажи историю" }],
  stream: true,
});
for await (const chunk of stream) {
  process.stdout.write(chunk.choices[0]?.delta?.content ?? "");
}
```

`break` из цикла или `stream.abort()` закрывают соединение. Если провайдер не
смог закончить ответ, сервер присылает ошибку внутри стрима — SDK бросает
`StreamError`; обрыв соединения посреди стрима — `NetworkError`. Отменить
обычный (не стрим) запрос можно через `signal: AbortSignal`.

### Vision

```ts
const reply = await client.chat.completions.create({
  model: "gpt-6-luna",
  messages: [{
    role: "user",
    content: [
      { type: "text", text: "Что на изображении?" },
      { type: "image_url", image_url: { url: "https://example.com/image.jpg" } },
    ],
  }],
});
```

## OpenAI-совместимые картинки и видео — `client.v1`

Нативные `client.images` / `client.videos` работают через `POST /generate`.
Те же модели доступны в форме OpenAI / Sora через `client.v1`:

```ts
// Модели, доступные ключу (GET /v1/models, нужен ключ)
const ids = (await client.v1.models.list()).map((m) => m.id);

// Картинка: сервер сам ждёт результат (до 180 с, у Seedream Pro до 540 с)
const img = await client.v1.images.generate({ model: "nano-banana", prompt: "Кот", size: "1024x1024" });
console.log(img.data[0].url);

// Видео: создание сразу возвращает объект, потом ждём
let video = await client.v1.videos.create({ model: "veo-3-fast", prompt: "Кот", seconds: 8, size: "1920x1080" });
video = await client.v1.videos.wait(video, 600_000); // или retrieve(video.id)
console.log(video.url);
```

`client.v1.chat` — тот же объект, что и `client.chat`.

## Настройки клиента

```ts
const client = new NexusClient({
  apiKey: "...",                        // по умолчанию NEXUSAPI_API_KEY
  baseUrl: "https://nexusapi.dev",      // по умолчанию NEXUSAPI_BASE_URL
  timeoutMs: 60_000,
  maxRetries: 2,                        // повторы при 429/5xx/сетевых ошибках
  defaultHeaders: { "X-Team": "media" },
  fetch: customFetch,                   // прокси, undici Agent, тесты
});

// Разовые настройки — копия клиента
await client.withOptions({ maxRetries: 0, defaultHeaders: { "X-Trace": "abc" } }).tasks.get(taskId);

// Или опции одного вызова — второй аргумент методов ресурсов
// (для task.wait / v1.videos.wait — { timeoutMs, pollIntervalMs, signal })
await client.videos.create(
  { model: "veo-3-fast", prompt: "…", duration: 8 },
  { signal: controller.signal, timeoutMs: 120_000, maxRetries: 1, headers: { "X-Trace": "abc" } },
);
```

`timeoutMs` клиента — лимит одной попытки (вместе с чтением ответа); у
`POST /generate` он не меньше 120 с, у чата и `v1.images` — не меньше 600 с.
`timeoutMs` вызова — общий лимит на весь вызов вместе с повторами. Без него
`v1.images.generate` при повторах 504 может ждать дольше 180 с (до ~9 минут);
ограничьте его `timeoutMs` или `maxRetries`.

Свой ключ идемпотентности можно передать в параметрах (`idempotencyKey`) или
заголовком `Idempotency-Key` вызова — SDK использует его во всех повторах.

> **ESM и CommonJS.** Пакет содержит обе сборки. Если в одном процессе его
> загрузили и через `import`, и через `require`, это две копии классов:
> `instanceof` ошибки одной копии против класса другой вернёт `false`. Держитесь
> одного способа импорта или проверяйте `err.name`.

## Вебхуки и проверка подписи

Передайте `webhook_url` при создании задачи — когда она завершится, NexusAPI
пришлёт POST с телом `{ task_id, status, result, error }` и подписью в
заголовках `X-Nexus-Timestamp` / `X-Nexus-Signature`. Проверяйте подпись по
**сырому** телу запроса:

```ts
import express from "express";
import { NexusClient, WebhookVerificationError } from "nexusapi-sdk";

const client = new NexusClient();
const app = express();

app.post("/nexus-callback", express.raw({ type: "application/json" }), async (req, res) => {
  try {
    const event = await client.webhooks.unwrap(req.body, req.headers);
    if (event.status === "completed") await saveResult(event.task_id, event.result);
    res.sendStatus(200);
  } catch (e) {
    if (e instanceof WebhookVerificationError) return res.sendStatus(400);
    throw e;
  }
});
```

`client.webhooks.unwrap` вычисляет секрет из ключа клиента. Если сервис,
принимающий вебхуки, не должен знать API-ключ, получите секрет один раз
(`await client.webhooks.secret()` → `whsec_…`) и проверяйте им:

```ts
import { unwrapWebhook } from "nexusapi-sdk";

const event = await unwrapWebhook(rawBody, request.headers, process.env.NEXUS_WEBHOOK_SECRET!);
```

Подпись старше 5 минут отклоняется (`toleranceSeconds` меняет допуск) — так
перехваченный запрос нельзя прислать повторно.

## Возможности

- **Auto-retries** — на 5xx, 429 и сетевые ошибки. По умолчанию до 3 попыток
  с экспоненциальным backoff (0.5s, 1s, 2s + jitter). Учитывает `Retry-After`
  (секунды, HTTP-дата, `retry-after-ms`; больше 60 с не ждёт). Настраивается
  через `maxRetries: N` в опциях клиента. Паузу между повторами прерывает `signal`.
- **Auto Idempotency-Key** — для каждого POST автоматически генерируется
  UUID v4, если не передан свой (8–256 печатных ASCII-символов). Генерации
  (`/generate`, `/v1/images`, `/v1/videos`) сервер привязывает к ключу: повтор
  с тем же ключом — после 5xx, таймаута или обрыва — возвращает ту же задачу и
  не списывает деньги второй раз. Если повтор получил 409 «первый запрос ещё
  выполняется», SDK ждёт до 2 минут его ответ.
- **Что делать после ошибки.** У ошибок POST-запросов есть `idempotencyKey`,
  у ошибок с ответом сервера — `requestId`.
  - Генерации: повторяйте с тем же `idempotencyKey`, а не с новым — сервер
    вернёт ту же задачу. 504 у `v1.images.generate` несёт `taskId`: задачу можно
    дождаться через `(await client.tasks.get(err.taskId)).wait()`.
  - Чат: сервер хранит только успешный ответ — после 5xx повтор вызовет
    модель снова. Поэтому 500 и 503 SDK у чата сам не повторяет, а остальные
    повторы делает только в первые 9 минут вызова, пока сервер держит замок
    ключа.
  - Стрим чата повторить нельзя: сервер его не сохраняет.
  - В поддержку пишите с `requestId` — по нему находят запрос в логах.
- **Таймауты** — см. «Настройки клиента»: таймаут клиента — на попытку,
  `timeoutMs` вызова — на весь вызов. Таймаут — `APITimeoutError`.
- **Ожидание задач** — `task.wait()` и `client.v1.videos.wait()` пережидают
  временные ошибки опроса (сеть, 429, 5xx) и принимают
  `{ timeoutMs, pollIntervalMs, signal }`. Если время вышло, задача продолжает
  идти: `TaskTimeoutError` несёт `.taskId` и `.task` — дождитесь её ещё раз, а
  не создавайте новую. `images.generate` по умолчанию ждёт до 600 с.
- **Ключ не утекает в логи** — `console.log(client)` и `JSON.stringify(task)`
  не показывают API-ключ.
- **Лимит fetch в Node** — встроенный `fetch` ждёт заголовки ответа не дольше
  300 с (`APITimeoutError`). Для генераций, которые могут идти дольше
  (например, Seedream Pro), используйте нативный `client.images.generate`
  (опрос задачи) или стрим чата; либо передайте свой `fetch` с undici
  `Agent({ headersTimeout: 0 })`.
- **Все native-параметры** — `webhook_url`, `negative_prompt`, `image_url`,
  `cfg_scale`, `video_urls` для seedance, `source_video_url` для veo-extend
  передаются прямо в `create()` / `generate()` как обычные поля объекта.
- **Auto-polling** для image-моделей (sync UX). Для видео — `task.wait()`.
- **Типизированные ошибки** — все наследуют `NexusError`:
  - `APIStatusError` — любой HTTP-ответ с ошибкой; подклассы
    `ValidationError` (400/422), `AuthenticationError` (401), `BillingError`
    (402), `PermissionError` (403), `NotFoundError` (404), `ConflictError`
    (409), `RateLimitError` (429), `InternalServerError` (5xx);
  - `NetworkError` (ответа нет, причина в `.cause`) и его подкласс
    `APITimeoutError`;
  - `APIResponseValidationError`, `StreamError`, `TaskTimeoutError`,
    `TaskFailedError`, `WebhookVerificationError`.

  Поля: `status`, `headers`, `responseBody`, `requestId`, `code`, `type`,
  `param`, `idempotencyKey`, у ошибок про задачу — `taskId`.
- **TypeScript-first** — полные типы для всех методов; проверено
  arethetypeswrong для `import` и `require`.
- **Zero dependencies** — только встроенные `fetch` и WebCrypto.

## Примеры

### Webhook вместо polling

```ts
const task = await client.videos.create({
  model: "veo-3-fast",
  prompt: "...",
  duration: 8,
  webhook_url: "https://your-app.com/nexus-callback",
});
// Не вызываем .wait() — NexusAPI пришлёт подписанный POST, когда видео готово
return task.id;  // сохраняем в БД, ждём колбэка
```

Как проверить подпись вебхука — в разделе «Вебхуки и проверка подписи».

### Image-to-video (Kling motion)

```ts
const video = await client.videos.create({
  model: "kling-v2.6-motion-1080p",
  prompt: "Камера медленно отъезжает, раскрывая пейзаж",
  duration: 8,
  image_url: "https://your-bucket.s3.../start-frame.jpg",
});
await video.wait();
console.log(video.videoUrl);
```

### Image edit (nano-banana)

```ts
const edited = await client.images.generate({
  model: "nano-banana-pro",
  prompt: "Сделай небо более драматичным",
  image_url: "https://your-bucket.s3.../photo.jpg",
});
console.log(edited.imageUrl);
```

### Извлечение результата

```ts
const task = await client.videos.create({ ... });
await task.wait();
console.log(task.videoUrl);       // для видео-моделей
console.log(task.imageUrl);       // для image-моделей (берёт первое из imageUrls)
console.log(task.imageUrls);      // для image-моделей с несколькими изображениями
console.log(task.result);         // сырой объект с провайдер-специфичным контентом
```

### Обработка ошибок

```ts
import {
  APIStatusError,
  BillingError,
  NetworkError,
  RateLimitError,
  ValidationError,
} from "nexusapi-sdk";

try {
  const task = await client.videos.create({ ... });
  await task.wait();
} catch (e) {
  if (e instanceof BillingError) {
    console.error("Недостаточно средств");
  } else if (e instanceof RateLimitError) {
    console.error("Превышен rate-limit");
  } else if (e instanceof ValidationError) {
    console.error("Невалидные параметры:", e.message);
  } else if (e instanceof APIStatusError) {
    console.error(`Ошибка API ${e.status}, requestId=${e.requestId}`);
  } else if (e instanceof NetworkError) {
    // повторять — с тем же ключом: сервер вернёт ту же задачу
    console.error(`Нет ответа; повторите с idempotencyKey=${e.idempotencyKey}`);
  } else {
    throw e;
  }
}
```

### Каталог моделей

```ts
const models = await client.models.list();
for (const m of models) {
  console.log(`${m.id}: ${m.priceRub}₽ за ${m.unit}`);
}
```

## Документация

- [docs.nexusapi.dev](https://docs.nexusapi.dev) — гайды и каталог моделей
- [docs.nexusapi.dev/api-reference](https://docs.nexusapi.dev/api-reference) — полный API reference
- [docs.nexusapi.dev/models](https://docs.nexusapi.dev/models) — параметры каждой модели

## Лицензия

MIT
