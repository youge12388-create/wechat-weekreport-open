"""端到端验收用的假企微端点：只回 200，不碰真实企微。

用法：独立端口起这个脚本，设 WECOM_BASE_URL 指过来，然后跑真实链路。
投递模式可通过 /__mode 切换，用来验证「errcode=0 但某人不可投递」这一分支。
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

# 假企微记录每次消息发送，供验收核对「到底发给了谁」
SENT: list[dict] = []
INVALID_USERS = {"__set__": ""}
# getuserinfo 返回的 userid，默认对齐验收库里张三的 external_id
LOGIN_USERID = {"value": "u1"}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args) -> None:  # 静音，避免刷屏
        return

    def _json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path.startswith("/cgi-bin/gettoken"):
            self._json({"errcode": 0, "errmsg": "ok", "access_token": "fake-token", "expires_in": 7200})
        elif self.path.startswith("/__sent"):
            self._json({"sent": SENT, "invalid": INVALID_USERS["__set__"]})
        elif self.path.startswith("/cgi-bin/auth/getuserinfo"):
            self._json(
                {"errcode": 0, "errmsg": "ok", "userid": LOGIN_USERID["value"]}
            )
        else:
            self._json({"errcode": 0, "errmsg": "ok"})

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode("utf-8") if length else "{}"
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            payload = {}
        if self.path.startswith("/__mode"):
            INVALID_USERS["__set__"] = payload.get("invalid", "")
            self._json({"ok": True})
            return
        if self.path.startswith("/cgi-bin/message/send"):
            SENT.append(
                {
                    "touser": payload.get("touser", ""),
                    "title": (payload.get("textcard") or {}).get("title", ""),
                    "url": (payload.get("textcard") or {}).get("url", ""),
                }
            )
            self._json(
                {
                    "errcode": 0,
                    "errmsg": "ok",
                    "invaliduser": INVALID_USERS["__set__"],
                }
            )
            return
        self._json({"errcode": 0, "errmsg": "ok"})


def main() -> None:
    server = HTTPServer(("127.0.0.1", 8099), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print("fake wecom on 127.0.0.1:8099", flush=True)
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    main()
