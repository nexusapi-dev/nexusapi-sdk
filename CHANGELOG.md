# Changelog

Версии Python (`nexusapi-sdk` на PyPI) и Node (`nexusapi-sdk` на npm) выходят
вместе и с одинаковыми номерами. [SemVer](https://semver.org/).

## 0.3.1 — 2026-11-28

Исправления надёжности по аудиту; публичный интерфейс 0.3.0 не меняется.

Деньги и задачи:
- Если повтор самого SDK получил 409 «первый запрос с этим ключом ещё
  выполняется», SDK ждёт его сохранённый ответ (до 2 минут), а не бросает
  `ConflictError`. Раньше медленный `POST /generate` мог оставить оплаченную
  задачу без task_id у пользователя.
- `POST /generate` и `/v1/videos` ждут ответа не меньше 120 с (часть моделей
  до ответа проверяет входные файлы до 90 с), а не 60.
- `images.generate` по умолчанию ждёт до 600 с (Seedream Pro идёт до 540 с).
- `task.wait()` и `v1.videos.wait()` пережидают временные ошибки опроса (сеть,
  429, 5xx); ошибки ожидания несут `task_id`/`task` (`taskId`/`task` в Node),
  чтобы задачу можно было дождаться, а не создавать заново.
- Сообщения об ошибках больше не обещают безопасный повтор там, где сервер не
  сохраняет ответ (5xx, стрим чата).

Надёжность:
- Retry-After: секунды, HTTP-дата и `retry-after-ms`; отрицательные,
  бесконечные и больше 60 с значения игнорируются (раньше Python падал на
  `-1`, а `3600` означал час ожидания).
- Ответ не в JSON (например, HTML-страница прокси) или без `task_id` —
  новое исключение `APIResponseValidationError` вместо `JSONDecodeError` /
  `KeyError` / `SyntaxError`.
- Разбор ошибок не падает на нестандартных телах, сообщение обрезается до
  500 символов; у ошибок есть `headers`.
- id задачи экранируется в URL (`tasks.get`).
- Проверка до отправки: длина `idempotency_key` (8–256), `base_url` должен
  быть http(s); `base_url` с `/v1` на конце исправляется с предупреждением.
- Python: `with NexusClient(...) as c` типизирован; повторы логируются на INFO;
  свойства задачи не падают на результате не-dict; `audio_url`/`audio_urls`.
- Node: `timeoutMs` и `signal` покрывают чтение тела ответа и паузы между
  повторами; API-ключ не виден в `console.log`/`util.inspect`;
  `JSON.stringify(task)` работает; у `NetworkError` есть `cause`; ключ
  идемпотентности без статического `node:crypto` (сборка под браузер/edge);
  `signal` больше не попадает в тело запроса `v1.images`/`v1.videos`;
  `task.wait({ timeoutMs, pollIntervalMs, signal })`; `audioUrl`/`audioUrls`.

Выпуск:
- Публикация запускает полный CI и проверяет, что тег на `main`; сборка
  npm-пакета отделена от задачи с правом публикации; Actions закреплены по SHA.
- Python: лицензия в формате PEP 639, `httpx < 1`, классификатор 3.14.

## 0.3.0

Добавлено (обратно совместимо с 0.2.0):

- `client.chat.completions.create(...)` — `POST /v1/chat/completions`, ответ в
  формате OpenAI; `stream=True` / `stream: true` — SSE-стрим чанков
  (`Stream` / `AsyncStream` в Python, `Stream` в Node).
- `client.v1` — OpenAI-совместимые эндпоинты: `v1.models.list()`
  (`GET /v1/models`), `v1.images.generate(...)` (`POST /v1/images/generations`),
  `v1.videos.create(...)`, `v1.videos.retrieve(id)`, `v1.videos.wait(video)`.
  `client.v1.chat` — тот же объект, что `client.chat`.
- Ошибки `ConflictError` (409: запрос с этим `Idempotency-Key` ещё выполняется)
  и `StreamError` (ошибка, присланная сервером внутри стрима).
- Python: файл `py.typed`; Node: экспорт `VERSION` и типов для чата и `/v1`.

Изменено:

- Чат и `v1.images.generate` ждут ответа не меньше 600 с (настраивается
  `timeout=` / `timeoutMs`), подключение — по обычному таймауту клиента.
- Для этих долгих запросов ответы 500/502/504 не повторяются: задача на
  сервере могла уже начаться, и повтор оплатил бы её второй раз. 429 и 503
  повторяются как раньше.
- Стрим чата: `Idempotency-Key` отправляется только если передан явно;
  повтор — только если запрос не дошёл до сервера. Долгие запросы (чат,
  `v1.images.generate`) после обрыва уже отправленного запроса тоже не
  повторяются: `NetworkError` несёт `idempotency_key` / `idempotencyKey`,
  повтор с ним возвращает результат без второго списания.
- У всех ошибок есть `idempotency_key` / `idempotencyKey` запроса.
- Node: ключ идемпотентности генерируется через `node:crypto`, а не через
  глобальный `crypto`, который в Node 18 включается флагом.
- Node: текст не-JSON ответа с ошибкой (например, от прокси) попадает в
  сообщение исключения. Python: `httpx.RemoteProtocolError` и другие ошибки
  транспорта оборачиваются в `NetworkError`.
- Node: тип `ModelInfo` описывает текущий каталог (`kind: "llm" | "audio"`,
  цены за токены, `pricingOptions`), `TaskStatus` включает `"queued"`;
  пустое тело успешного ответа больше не роняет разбор JSON.

## 0.2.0

- Python: async-клиент `AsyncNexusClient`.
- Повторы при 429/5xx/сетевых ошибках с экспоненциальной задержкой и
  `Retry-After`.
- Автоматический `Idempotency-Key` (`sdk-<uuid>`) для каждого POST.
- Тесты на моках.

## 0.1.0

- Первый выпуск: `videos.create`, `images.generate`, `tasks.get`, `models.list`,
  `Task.wait()`.
