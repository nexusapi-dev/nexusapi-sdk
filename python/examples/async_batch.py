"""Несколько картинок параллельно на AsyncNexusClient.

    export NEXUSAPI_API_KEY=...
    python examples/async_batch.py
"""
import asyncio

from nexusapi import AsyncNexusClient, NexusError

PROMPTS = ["Лиса в лесу", "Маяк на закате", "Чашка кофе, вид сверху"]


async def main() -> None:
    async with AsyncNexusClient() as client:
        results = await asyncio.gather(
            *(client.images.generate(model="nano-banana", prompt=p) for p in PROMPTS),
            return_exceptions=True,
        )
        for prompt, result in zip(PROMPTS, results):
            if isinstance(result, NexusError):
                print(f"{prompt}: ошибка {type(result).__name__}: {result}")
            elif isinstance(result, BaseException):
                raise result
            else:
                print(f"{prompt}: {result.image_url}")


if __name__ == "__main__":
    asyncio.run(main())
