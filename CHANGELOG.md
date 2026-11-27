# Changelog

Версии Python (`nexusapi-sdk` на PyPI) и Node (`nexusapi-sdk` на npm) выходят
вместе и с одинаковыми номерами. [SemVer](https://semver.org/).

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
