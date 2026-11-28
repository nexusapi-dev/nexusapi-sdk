"""Приём вебхуков с проверкой подписи — на стандартной библиотеке, без фреймворков.

    export NEXUSAPI_WEBHOOK_SECRET=whsec_...   # client.webhooks.secret()
    python examples/webhook_server.py          # слушает :8080

В FastAPI то же самое: body = await request.body(); webhooks.unwrap(body, request.headers, secret).
"""
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from nexusapi import webhooks

SECRET = os.environ["NEXUSAPI_WEBHOOK_SECRET"]
MAX_BODY_BYTES = 1024 * 1024  # события маленькие; больше — не от NexusAPI


class Handler(BaseHTTPRequestHandler):
    timeout = 10  # медленный клиент не держит соединение вечно

    def _reply(self, status: int) -> None:
        self.send_response(status)
        self.end_headers()

    def do_POST(self) -> None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return self._reply(400)
        if not 0 <= length <= MAX_BODY_BYTES:
            return self._reply(413)
        body = self.rfile.read(length)  # сырые байты: подпись считается по ним
        try:
            event = webhooks.unwrap(body, dict(self.headers), SECRET)
        except webhooks.WebhookVerificationError as e:
            print("отклонён:", e)
            return self._reply(400)
        print("задача", event.get("task_id"), "→", event.get("status"), event.get("result"))
        self._reply(200)


if __name__ == "__main__":
    # Многопоточный сервер: один медленный запрос не задерживает остальные.
    ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
