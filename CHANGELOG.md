# Changelog

Версии Python (`nexusapi-sdk` на PyPI) и Node (`nexusapi-sdk` на npm) выходят
вместе и с одинаковыми номерами. [SemVer](https://semver.org/).

## 0.5.0 — 2026-11-28

Добавлено (обратно совместимо с 0.4.0):

- Помощники стрима чата: `stream.text()` — весь текст ответа,
  `stream.final_completion()` / `stream.finalCompletion()` — обычный ответ
  `chat.completion` по тем же правилам, что сервер собирает обычный ответ: текст,
  reasoning, refusal, logprobs, вызовы инструментов (аргументы собираются из
  кусков), `finish_reason` и `usage`. Учитывают чанки, уже прочитанные в цикле;
  повторный вызов возвращает то же; ошибку стрима бросают снова.
- Примеры: `python/examples/` и `node/examples/` (картинка и видео, чат и
  стрим, обработка ошибок с повтором по ключу, приём вебхуков, параллельные
  запросы). CI проверяет их типы.
- Смоук-тест против живого API: `smoke/` (бесплатные проверки и платные
  на ~1 ₽ с флагом `--paid`).
- GitHub Releases создаются автоматически при публикации, с заметками из
  CHANGELOG и файлами пакета.
- Примеры вебхуков устойчивы к оборванным и медленным запросам и ограничивают
  размер тела.

Изменено:

- Сервер держит замок Idempotency-Key 15 минут (было 10), поэтому окно
  повторов чата расширено до 14 минут (было 9).

Исправлено:

- Повтор чата мог уйти позже окна: проверялось только время решения, без паузы
  перед повтором (Retry-After до 60 с). Теперь в окно должен попасть сам повтор.
- Node: дедлайны вызова и окно повторов считаются по монотонным часам
  (`performance.now()`), перевод системного времени их не сдвигает.

## 0.4.0 — 2026-11-28

### Миграция с 0.3.x

- **Python ≥ 3.10, Node.js ≥ 20.** Python 3.9 и Node 18 больше не
  поддерживаются (у обоих закончилась поддержка от сообщества).
- **Python, типы:** ответы `/v1` и `models.list()` описаны TypedDict
  (`nexusapi.types`). Во время выполнения это те же dict, но под mypy/pyright
  теперь ошибка:
  - передать ответ туда, где ждут `dict[str, Any]` / `list[dict[str, Any]]`;
  - обратиться через `[...]` к полю, которого нет в TypedDict (`.get()` работает);
  - `url: str = video["url"]` — поле `Optional[str]`.

  Используйте типы из `nexusapi.types`, `Mapping[str, Any]` или `.get()`.
- **`api_key=""`** теперь ошибка, как и раньше; ключ из `NEXUSAPI_API_KEY`
  берётся только если `api_key` не передан (None).
- **Повторы:** сервер теперь привязывает Idempotency-Key к задаче, поэтому SDK
  повторяет с тем же ключом то, что раньше не повторял: 502/503/504 и обрыв у
  `v1.images.generate`, 502/504 и обрыв у чата (только в первые 9 минут
  вызова). Второго списания с баланса это не создаёт. У чата SDK больше не
  повторяет 503: такой повтор заново отправлял запрос платному поставщику
  модели. 500 у чата и `v1.images` не повторяется.
- **Таймауты:** явный `timeout=` / `timeoutMs` у метода — теперь общий лимит
  на вызов вместе с повторами; таймаут клиента — на одну попытку.
- Иерархия ошибок расширена, но совместима: всё по-прежнему `NexusError`, а
  HTTP-ошибки теперь ещё и `APIStatusError`.

### Добавлено

- Ключ и адрес API из переменных окружения `NEXUSAPI_API_KEY` и
  `NEXUSAPI_BASE_URL`: `NexusClient()` без аргументов.
- `default_headers` / `defaultHeaders`; Python — `http_client=` (свой
  httpx-клиент: прокси, TLS, лимиты).
- `with_options(...)` / `withOptions(...)` — копия клиента с другим
  таймаутом, числом повторов и заголовками. Node: второй аргумент
  `{ signal, timeoutMs, maxRetries, headers }` у каждого метода.
- Ошибки: `APIStatusError` (база HTTP-ошибок), `InternalServerError` (5xx),
  `APITimeoutError` (подкласс `NetworkError`), `WebhookVerificationError`;
  поля `request_id` / `requestId` (из `X-Request-Id`), `code`, `type`, `param`;
  у 504 от `/v1/images` — `task_id` / `taskId` задачи, которая ещё идёт.
- Вебхуки: проверка подписи `X-Nexus-Signature` (HMAC-SHA256 + timestamp):
  `client.webhooks.unwrap(body, headers)`, `client.webhooks.secret()`, отдельные
  функции `nexusapi.webhooks.unwrap/verify_signature` (Python) и
  `unwrapWebhook/verifyWebhookSignature/webhookSecretFromApiKey` (Node).
- Python: типы ответов `nexusapi.types`, пакет проходит `mypy --strict`;
  `Task` сериализуется pickle (без клиента); User-Agent с версией Python.
- Node: сборки ESM и CommonJS (`require("nexusapi-sdk")`), проверены
  arethetypeswrong и publint; работает под браузер/edge без `node:` импортов;
  User-Agent с версией Node.

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
