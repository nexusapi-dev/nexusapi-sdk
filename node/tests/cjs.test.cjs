/**
 * CommonJS: require("nexusapi-sdk") работает и отдаёт те же классы.
 */
const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const pkg = require("../package.json");
const sdk = require("../dist/cjs/index.js");

describe("CommonJS build", () => {
  it("exports the client, errors and helpers", () => {
    assert.equal(typeof sdk.NexusClient, "function");
    assert.equal(sdk.VERSION, pkg.version);
    const err = new sdk.RateLimitError("slow down", 429);
    assert.ok(err instanceof sdk.APIStatusError);
    assert.ok(err instanceof sdk.NexusError);
    assert.equal(typeof sdk.unwrapWebhook, "function");
  });

  it("makes requests", async () => {
    const calls = [];
    const client = new sdk.NexusClient({
      apiKey: "k",
      baseUrl: "https://api.test",
      fetch: async (url) => {
        calls.push(String(url));
        return new Response(JSON.stringify({ task_id: "t", status: "pending" }), { status: 200 });
      },
    });
    const task = await client.tasks.get("t");
    assert.equal(task.id, "t");
    assert.equal(calls[0], "https://api.test/tasks/t");
  });
});
