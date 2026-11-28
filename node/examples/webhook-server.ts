/**
 * Приём вебхуков с проверкой подписи — на node:http, без фреймворков.
 *
 *   export NEXUSAPI_WEBHOOK_SECRET=whsec_...   # await client.webhooks.secret()
 *   npx tsx examples/webhook-server.ts         # слушает :8080
 *
 * В Express то же самое: express.raw({ type: "application/json" }) и
 * unwrapWebhook(req.body, req.headers, secret).
 */
import { createServer, type IncomingMessage } from "node:http";
import { unwrapWebhook, WebhookVerificationError } from "nexusapi-sdk";

const secret = process.env.NEXUSAPI_WEBHOOK_SECRET;
if (!secret) throw new Error("set NEXUSAPI_WEBHOOK_SECRET");

const MAX_BODY_BYTES = 1024 * 1024; // события маленькие; больше — не от NexusAPI

/** Сырые байты тела: подпись считается по ним, не по распарсенному JSON. */
async function readBody(req: IncomingMessage): Promise<Buffer> {
  const parts: Buffer[] = [];
  let size = 0;
  for await (const part of req) {
    size += (part as Buffer).length;
    if (size > MAX_BODY_BYTES) throw new Error("body too large");
    parts.push(part as Buffer);
  }
  return Buffer.concat(parts);
}

const server = createServer(async (req, res) => {
  // Весь обработчик в try: оборванное соединение или кривой запрос не должны
  // ронять процесс необработанной ошибкой.
  try {
    const event = await unwrapWebhook(await readBody(req), req.headers, secret);
    console.log("задача", event.task_id, "→", event.status, event.result);
    res.writeHead(200).end();
  } catch (e) {
    if (e instanceof WebhookVerificationError) console.log("отклонён:", e.message);
    else console.error("не удалось принять вебхук:", e);
    if (!res.headersSent && !res.destroyed) res.writeHead(400).end();
  }
});
server.requestTimeout = 10_000; // медленный клиент не держит соединение вечно
server.listen(8080);
