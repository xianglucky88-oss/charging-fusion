"""离线脚本化 HTTP 服务器：只服务测试内置响应，不访问任何外部网络。

它按顺序记录每个请求的条件头与每次响应实际写出的字节数，用于核对
「哪个条件头对应哪份正文、304 是否真的没有下载正文」。
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


class ScriptedState:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.versions: list[dict[str, Any]] = []
        self.current: dict[str, Any] | None = None
        self.log: list[dict[str, Any]] = []
        # 交错请求控制：挂起第一个带条件头的请求，直到测试显式放行。
        self.hold_first_conditional = False
        self.arrived = threading.Event()
        self.resume = threading.Event()
        self._held_any = False
        # 无论当前版本是什么，这些 etag 一律以 304 应答。
        self.force_304_for: set[str] = set()
        # 为 True 时对任何请求都回 304（用于模拟无基线却收到 304）。
        self.always_304 = False
        # 为 True 时忽略条件头，一律返回当前版本的完整 200（模拟带内强制刷新）。
        self.ignore_conditional = False

    def set_version(self, body: bytes, etag: str | None, last_modified: str | None) -> dict[str, Any]:
        version = {
            "body": body,
            "etag": etag,
            "last_modified": last_modified,
            "digest": __import__("hashlib").sha256(body).hexdigest(),
        }
        with self.lock:
            self.versions.append(version)
            self.current = version
        return version

    def reset_events(self) -> None:
        self.arrived.clear()
        self.resume.clear()
        self._held_any = False


def _make_handler(state: ScriptedState) -> type[BaseHTTPRequestHandler]:
    class _Handler(BaseHTTPRequestHandler):
        def _record_response(self, seq: int, status: int, bytes_written: int, version: dict[str, Any] | None) -> None:
            with state.lock:
                state.log.append(
                    {
                        "seq": seq,
                        "kind": "response",
                        "status": status,
                        "bytes": bytes_written,
                        "etag": version["etag"] if version else None,
                        "last_modified": version["last_modified"] if version else None,
                        "digest": version["digest"] if version else None,
                    }
                )

        def _answer_304(self, seq: int) -> None:
            current = state.current
            self.send_response(304)
            if current and current.get("etag"):
                self.send_header("ETag", current["etag"])
            if current and current.get("last_modified"):
                self.send_header("Last-Modified", current["last_modified"])
            self.end_headers()  # 304 没有响应体
            self._record_response(seq, 304, 0, None)

        def _answer_200(self, seq: int) -> None:
            current = state.current
            assert current is not None
            body = current["body"]
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            if current.get("etag"):
                self.send_header("ETag", current["etag"])
            if current.get("last_modified"):
                self.send_header("Last-Modified", current["last_modified"])
            self.end_headers()
            self.wfile.write(body)
            self._record_response(seq, 200, len(body), current)

        def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 约定
            inm = self.headers.get("If-None-Match")
            ims = self.headers.get("If-Modified-Since")
            with state.lock:
                seq = len([e for e in state.log if e.get("kind") == "request"]) + 1
                state.log.append(
                    {
                        "seq": seq,
                        "kind": "request",
                        "path": self.path,
                        "if_none_match": inm,
                        "if_modified_since": ims,
                        "authorization": self.headers.get("Authorization"),
                        "x_api_key": self.headers.get("X-API-Key"),
                    }
                )

            if state.always_304:
                self._answer_304(seq)
                return

            if state.hold_first_conditional and (inm or ims) and not state.ignore_conditional:
                with state.lock:
                    should_hold = not state._held_any
                    state._held_any = True
                if should_hold:
                    state.arrived.set()
                    state.resume.wait(timeout=10)
                    self._answer_304(seq)
                    return

            if inm and inm in state.force_304_for and not state.ignore_conditional:
                self._answer_304(seq)
                return

            current = state.current
            assert current is not None
            if not state.ignore_conditional:
                if inm and current.get("etag") and inm == current["etag"]:
                    self._answer_304(seq)
                    return
                if ims and current.get("last_modified") and ims == current["last_modified"]:
                    self._answer_304(seq)
                    return
            self._answer_200(seq)

        def log_message(self, *_args: Any) -> None:
            return

    return _Handler


class ScriptedServer:
    def __init__(self) -> None:
        self.state = ScriptedState()
        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(self.state))
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self._httpd.server_port}/records"

    def url_with(self, query: str) -> str:
        return f"http://127.0.0.1:{self._httpd.server_port}/records?{query}"

    def version(self, payload: Any, etag: str | None, last_modified: str | None) -> dict[str, Any]:
        body = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        return self.state.set_version(body, etag, last_modified)

    @property
    def log(self) -> list[dict[str, Any]]:
        with self.state.lock:
            return list(self.state.log)

    def stop(self) -> None:
        self._httpd.shutdown()
        self._thread.join(timeout=5)
        self._httpd.server_close()
