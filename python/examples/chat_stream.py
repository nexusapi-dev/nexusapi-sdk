"""Текстовая модель: обычный ответ и стриминг.

    export NEXUSAPI_API_KEY=...
    python examples/chat_stream.py
"""
from nexusapi import NexusClient

MODEL = "gpt-6-luna"


def main() -> None:
    with NexusClient() as client:
        reply = client.chat.completions.create(
            model=MODEL,
            messages=[{"role": "user", "content": "Одной фразой: что такое идемпотентность?"}],
            max_tokens=300,
        )
        print(reply["choices"][0]["message"]["content"])
        print("usage:", reply.get("usage"))

        # Стрим: печатаем текст по мере генерации.
        with client.chat.completions.create(
            model=MODEL,
            messages=[{"role": "user", "content": "Перечисли три цвета радуги"}],
            stream=True,
        ) as stream:
            for chunk in stream:
                for choice in chunk.get("choices") or []:
                    print(choice.get("delta", {}).get("content") or "", end="", flush=True)
        print()

        # Только итог стрима — одним ответом.
        final = client.chat.completions.create(
            model=MODEL, messages=[{"role": "user", "content": "Скажи «готово»"}], stream=True,
        ).final_completion()
        print(final["choices"][0]["message"]["content"])


if __name__ == "__main__":
    main()
