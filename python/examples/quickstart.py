"""Картинка и видео через нативный API.

    export NEXUSAPI_API_KEY=...
    python examples/quickstart.py
"""
from nexusapi import NexusClient


def main() -> None:
    with NexusClient() as client:  # ключ из NEXUSAPI_API_KEY
        # Картинка: generate() сам дожидается результата.
        image = client.images.generate(model="nano-banana", prompt="Минималистичный логотип кофейни")
        print("image:", image.image_url)

        # Видео: create() сразу возвращает задачу, ждём отдельно.
        task = client.videos.create(model="veo-3-fast", prompt="Кот играет на пианино", duration=8)
        print("video task:", task.id)
        task.wait(timeout=600)
        print("video:", task.video_url)


if __name__ == "__main__":
    main()
