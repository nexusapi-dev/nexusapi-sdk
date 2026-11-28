/**
 * Картинка и видео через нативный API.
 *
 *   export NEXUSAPI_API_KEY=...
 *   npx tsx examples/quickstart.ts
 */
import { NexusClient } from "nexusapi-sdk";

const client = new NexusClient(); // ключ из NEXUSAPI_API_KEY

// Картинка: generate() сам дожидается результата.
const image = await client.images.generate({ model: "nano-banana", prompt: "Минималистичный логотип кофейни" });
console.log("image:", image.imageUrl);

// Видео: create() сразу возвращает задачу, ждём отдельно.
const task = await client.videos.create({ model: "veo-3-fast", prompt: "Кот играет на пианино", duration: 8 });
console.log("video task:", task.id);
await task.wait({ timeoutMs: 600_000 });
console.log("video:", task.videoUrl);
