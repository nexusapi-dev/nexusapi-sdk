# NexusAPI Node.js SDK

Официальный Node.js / TypeScript SDK для [NexusAPI](https://docs.nexusapi.dev) —
единый API для генерации видео и изображений (Veo, Kling, Seedance,
Nano-Banana и др.) и текстовых моделей (Claude, GPT) через OpenAI-совместимый
`/v1`.

## Установка

```bash
npm install nexusapi-sdk
```

Требует **Node.js 18+** (используем встроенный `fetch`). Без внешних зависимостей.

> Пакет на npm называется `nexusapi-sdk` (имя `nexusapi` зарезервировано
> другим проектом). Mirror Python SDK: `pip install nexusapi-sdk`.

## Quickstart

```ts
import { NexusClient } from "nexusapi-sdk";

const client = new NexusClient({
  apiKey: process.env.NEXUS_KEY!,  // создать в panel.nexusapi.dev
});

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

## Возможности

- **Auto-retries** — на 5xx, 429 и сетевые ошибки. По умолчанию до 3 попыток
  с экспоненциальным backoff (0.5s, 1s, 2s + jitter). Учитывает `Retry-After`
  (секунды, HTTP-дата, `retry-after-ms`; больше 60 с не ждёт). Настраивается
  через `maxRetries: N` в опциях клиента. Паузу между повторами прерывает `signal`.
- **Auto Idempotency-Key** — для каждого POST автоматически генерируется
  UUID v4, если не передан свой (8–256 печатных ASCII-символов). Повтор самого
  SDK с тем же ключом не создаёт вторую задачу; если повтор получил 409
  «первый запрос ещё выполняется», SDK ждёт до 2 минут его сохранённый ответ.
  Для стрима чата ключ уходит только если передан явно, а повтор делается
  только когда запрос не дошёл до сервера.
- **Что делать после ошибки.** У каждой ошибки есть `idempotencyKey`.
  - `NetworkError`, `ConflictError`, 4xx: повторяйте с тем же ключом, а не с
    новым — если первый запрос выполнился, сервер вернёт его результат.
  - 5xx: сервер ответ не сохраняет, запрос мог быть оплачен. Проверьте
    результат (`client.tasks.get`) прежде чем повторять.
  - Стрим чата повторить нельзя: сервер не сохраняет его результат.
- **Таймауты** — `timeoutMs` покрывает весь запрос вместе с чтением ответа.
  `POST /generate` ждёт не меньше 120 с (часть моделей до ответа проверяет
  входные файлы), `chat.completions` и `v1.images.generate` — не меньше 600 с
  (поле `timeoutMs`). Ответы 500, 502 и 504 долгих запросов и обрыв уже
  отправленного запроса SDK не повторяет: задача могла уже начаться, и
  повтор оплатил бы её второй раз.
- **Ожидание задач** — `task.wait()` и `client.v1.videos.wait()` пережидают
  временные ошибки опроса (сеть, 429, 5xx) и принимают
  `{ timeoutMs, pollIntervalMs, signal }`. Если время вышло, задача продолжает
  идти: `TaskTimeoutError` несёт `.taskId` и `.task` — дождитесь её ещё раз, а
  не создавайте новую. `images.generate` по умолчанию ждёт до 600 с.
- **Ключ не утекает в логи** — `console.log(client)` и `JSON.stringify(task)`
  не показывают API-ключ.
- **Лимит fetch в Node** — встроенный `fetch` ждёт заголовки ответа не дольше
  300 с. Для генераций, которые могут идти дольше (например, Seedream Pro),
  используйте нативный `client.images.generate` (опрос задачи) или стрим
  чата; либо передайте свой `fetch` с undici `Agent({ headersTimeout: 0 })`.
- **Все native-параметры** — `webhook_url`, `negative_prompt`, `image_url`,
  `cfg_scale`, `video_urls` для seedance, `source_video_url` для veo-extend
  передаются прямо в `create()` / `generate()` как обычные поля объекта.
- **Auto-polling** для image-моделей (sync UX). Для видео — `task.wait()`.
- **Типизированные ошибки** — `NexusError` (база), `BillingError`,
  `RateLimitError`, `ValidationError`, `NotFoundError`, `AuthenticationError`,
  `PermissionError`, `TaskTimeoutError` (client-side wait), `TaskFailedError`
  (провайдер не справился), `NetworkError` (retries исчерпаны),
  `ConflictError` (409: запрос с этим Idempotency-Key ещё выполняется),
  `StreamError` (ошибка внутри SSE-стрима), `APIResponseValidationError`
  (ответ не JSON или без нужного поля). У ошибок есть `status`, `headers`,
  `responseBody`, `idempotencyKey`, у сетевых — `cause`, у ошибок ожидания —
  `taskId`.
- **TypeScript-first** — полные типы для всех методов.
- **Zero dependencies** — только native `fetch` Node 18+.

## Примеры

### Webhook вместо polling

```ts
const task = await client.videos.create({
  model: "veo-3-fast",
  prompt: "...",
  duration: 8,
  webhook_url: "https://your-app.com/nexus-callback",
});
// Не вызываем .wait() — NexusAPI POST'нет на webhook_url когда видео готово
return task.id;  // сохраняем в БД, ждём колбэка
```

Вебхук не подписан: POST на ваш адрес может прислать кто угодно. Считайте его
только сигналом и берите результат из API:

```ts
async function onNexusCallback(payload: { task_id: string }) {
  const task = await client.tasks.get(payload.task_id); // источник правды — API
  if (task.status === "completed") await saveResult(task.id, task.videoUrl);
}
```

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
  NexusClient,
  BillingError,
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
