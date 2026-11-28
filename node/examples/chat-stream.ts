/**
 * Текстовая модель: обычный ответ и стриминг.
 *
 *   export NEXUSAPI_API_KEY=...
 *   npx tsx examples/chat-stream.ts
 */
import { NexusClient } from "nexusapi-sdk";

const client = new NexusClient();
const model = "gpt-6-luna";

const reply = await client.chat.completions.create({
  model,
  messages: [{ role: "user", content: "Одной фразой: что такое идемпотентность?" }],
  max_tokens: 300,
});
console.log(reply.choices[0]?.message.content);
console.log("usage:", reply.usage);

// Стрим: печатаем текст по мере генерации.
const stream = await client.chat.completions.create({
  model,
  messages: [{ role: "user", content: "Перечисли три цвета радуги" }],
  stream: true,
});
for await (const chunk of stream) {
  process.stdout.write(chunk.choices[0]?.delta?.content ?? "");
}
process.stdout.write("\n");

// Только итог стрима — одним ответом.
const final = await (
  await client.chat.completions.create({ model, messages: [{ role: "user", content: "Скажи «готово»" }], stream: true })
).finalCompletion();
console.log(final.choices[0]?.message.content);
