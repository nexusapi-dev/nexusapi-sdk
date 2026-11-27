# NexusAPI SDKs

Официальные клиентские библиотеки [NexusAPI](https://docs.nexusapi.dev) — единого API
для генерации видео и изображений (Veo, Kling, Seedance, Nano-Banana и др.) и
текстовых моделей (Claude, GPT).

[![CI](https://github.com/nexusapi-dev/nexusapi-sdk/actions/workflows/ci.yml/badge.svg)](https://github.com/nexusapi-dev/nexusapi-sdk/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

| Язык | Пакет | Каталог | Установка |
|---|---|---|---|
| Python 3.9+ | [![PyPI](https://img.shields.io/pypi/v/nexusapi-sdk)](https://pypi.org/project/nexusapi-sdk/) | [`python/`](python/) | `pip install nexusapi-sdk` |
| Node.js 18+ / TypeScript | [![npm](https://img.shields.io/npm/v/nexusapi-sdk)](https://www.npmjs.com/package/nexusapi-sdk) | [`node/`](node/) | `npm install nexusapi-sdk` |

Оба SDK умеют:

- нативный API — `POST /generate`, `GET /tasks/{id}`, `GET /public/models`:
  `client.videos.create(...)`, `client.images.generate(...)`, `task.wait()`;
- OpenAI-совместимый `/v1` — `client.chat.completions.create(...)` (в том
  числе SSE-стрим), `client.v1.models.list()`, `client.v1.images.generate(...)`,
  `client.v1.videos.create(...)` / `retrieve` / `wait`;
- повторы при 429/5xx/сетевых ошибках и автоматический `Idempotency-Key`,
  чтобы повтор не создал вторую задачу и второе списание;
- типизированные ошибки (`BillingError`, `RateLimitError`, `ValidationError`, ...).

```python
from nexusapi import NexusClient

with NexusClient(api_key="ВАШ_КЛЮЧ") as client:
    reply = client.chat.completions.create(
        model="claude-sonnet-5",
        messages=[{"role": "user", "content": "Привет!"}],
    )
    print(reply["choices"][0]["message"]["content"])
```

```ts
import { NexusClient } from "nexusapi-sdk";

const client = new NexusClient({ apiKey: process.env.NEXUS_KEY! });
const task = await client.videos.create({ model: "veo-3-fast", prompt: "Кот играет на пианино", duration: 8 });
await task.wait();
console.log(task.videoUrl);
```

Ключ API создаётся в [panel.nexusapi.dev](https://panel.nexusapi.dev).
Подробности и примеры — в README каждого пакета.

## Документация

- [docs.nexusapi.dev/sdks](https://docs.nexusapi.dev/sdks/) — SDK
- [docs.nexusapi.dev/openai-compat](https://docs.nexusapi.dev/openai-compat/) — OpenAI-совместимый `/v1`
- [docs.nexusapi.dev/models](https://docs.nexusapi.dev/models/) — модели и их параметры
- [CHANGELOG.md](CHANGELOG.md) — история версий

## Разработка

```bash
# Python
cd python && python -m pip install -e ".[test]" && python -m pytest

# Node
cd node && npm ci && npm test
```

Тесты не ходят в сеть — HTTP подменяется моками. Выпуск версий описан в
[RELEASING.md](RELEASING.md), уязвимости — [SECURITY.md](SECURITY.md).

## Поддерживаемые версии

| | Поддерживается | Тестируется в CI |
|---|---|---|
| Python | 3.9+ | 3.9–3.14 |
| Node.js | 18+ | 18, 20, 22, 24, 26 |

Python 3.9, Node 18 и Node 20 уже без поддержки от своих сообществ; их
планируем убрать в 0.4.0. Исправления безопасности выходят для последней
минорной версии.

## Лицензия

[MIT](LICENSE)
