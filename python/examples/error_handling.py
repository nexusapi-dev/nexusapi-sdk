"""Обработка ошибок и безопасный повтор с тем же ключом идемпотентности.

    export NEXUSAPI_API_KEY=...
    python examples/error_handling.py
"""
import time
import uuid

from nexusapi import (
    APIStatusError,
    BillingError,
    ConflictError,
    NetworkError,
    NexusClient,
    RateLimitError,
    Task,
    ValidationError,
)


def create_video(client: NexusClient, order_id: str) -> Task:
    # Свой ключ (например, id заказа) — повтор с ним не создаст вторую задачу
    # и не спишет деньги второй раз, даже если первый ответ потерялся.
    key = f"order-{order_id}"
    for attempt in range(5):
        try:
            return client.videos.create(
                model="veo-3-fast", prompt="Кот играет на пианино", duration=8, idempotency_key=key,
            )
        except NetworkError as e:
            print(f"нет ответа, повторяю с тем же ключом {e.idempotency_key}")
        except ConflictError:
            # 409: первый запрос с этим ключом ещё выполняется на сервере.
            # Ждём и спрашиваем снова — придёт его же ответ, а не новая задача.
            print("первый запрос ещё выполняется, жду")
        time.sleep(2 ** attempt)
    raise RuntimeError("API недоступен")


def main() -> None:
    with NexusClient() as client:
        try:
            task = create_video(client, order_id=uuid.uuid4().hex)
            task.wait()
            print(task.video_url)
        except BillingError:
            print("Недостаточно средств — пополните баланс")
        except RateLimitError:
            print("Превышен лимит запросов ключа — подождите")
        except ValidationError as e:
            print(f"Неверные параметры: {e}")
        except APIStatusError as e:
            # request_id — для обращения в поддержку
            print(f"Ошибка API {e.status_code}: {e} (request_id={e.request_id})")


if __name__ == "__main__":
    main()
