/**
 * Обработка ошибок и безопасный повтор с тем же ключом идемпотентности.
 *
 *   export NEXUSAPI_API_KEY=...
 *   npx tsx examples/error-handling.ts
 */
import { randomUUID } from "node:crypto";
import { setTimeout as sleep } from "node:timers/promises";
import {
  APIStatusError,
  BillingError,
  ConflictError,
  NetworkError,
  NexusClient,
  RateLimitError,
  ValidationError,
  type Task,
} from "nexusapi-sdk";

const client = new NexusClient();

async function createVideo(orderId: string): Promise<Task> {
  // Свой ключ (например, id заказа) — повтор с ним не создаст вторую задачу
  // и не спишет деньги второй раз, даже если первый ответ потерялся.
  const idempotencyKey = `order-${orderId}`;
  for (let attempt = 0; attempt < 5; attempt += 1) {
    try {
      return await client.videos.create({ model: "veo-3-fast", prompt: "Кот играет на пианино", duration: 8, idempotencyKey });
    } catch (e) {
      if (e instanceof NetworkError) console.log(`нет ответа, повторяю с тем же ключом ${e.idempotencyKey}`);
      // 409: первый запрос с этим ключом ещё выполняется на сервере. Ждём и
      // спрашиваем снова — придёт его же ответ, а не новая задача.
      else if (e instanceof ConflictError) console.log("первый запрос ещё выполняется, жду");
      else throw e;
    }
    await sleep(1000 * 2 ** attempt);
  }
  throw new Error("API недоступен");
}

try {
  const task = await createVideo(randomUUID());
  await task.wait();
  console.log(task.videoUrl);
} catch (e) {
  if (e instanceof BillingError) console.log("Недостаточно средств — пополните баланс");
  else if (e instanceof RateLimitError) console.log("Превышен лимит запросов ключа — подождите");
  else if (e instanceof ValidationError) console.log(`Неверные параметры: ${e.message}`);
  else if (e instanceof APIStatusError) console.log(`Ошибка API ${e.status}: ${e.message} (requestId=${e.requestId})`);
  else throw e;
}
