"""A small synchronous client for the tests: calls the server's tools over HTTP the way a
script in another process would, with only the standard library.

    from http_client import Phone

    with Phone(port=port, token=token) as phone:
        phone.read_text()
        phone.tap(10, 10)

Refusals become exceptions: Busy, ScreenChanged (NOT DONE), TextTimeout, PhoneError.
"""

from __future__ import annotations

import base64
import http.client
import json
import os
import re
import time
from pathlib import Path
from typing import Any, Optional

DEFAULT_PORT = 47321
# Below the server's keep-alive timeout (uvicorn's 5 s).
_KEEPALIVE_S = 4.0
_SDK_PREFIX = re.compile(r"^Error executing tool \w+: ")
# Longer than the server's wait for the phone (PHONECTL_WAIT, 180 s) plus the slowest call.
TIMEOUT_S = 600.0


class PhoneError(RuntimeError):
    """A tool call failed; the message is the server's."""


class Busy(PhoneError):
    """Another client kept the phone for the whole wait."""


class ScreenChanged(PhoneError):
    """Someone else used the phone since this script last looked; the input was not sent."""


class TextTimeout(PhoneError):
    """wait_text / tap_text: the text did not show (or go) within the timeout."""


class Phone:
    def __init__(self, name: Optional[str] = None, port: Optional[int] = None,
                 token: Optional[str] = None, timeout_s: float = TIMEOUT_S):
        """name: how other clients see this script (X-Phonectl-Client); by default its process."""
        self.port = port or int(os.environ.get("PHONECTL_PORT", DEFAULT_PORT))
        if token is None:
            path = Path(os.environ.get("PHONECTL_TOKEN_FILE", Path.home() / ".config/phonectl/token")).expanduser()
            token = path.read_text().strip()
        self._headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": "2025-06-18",
        }
        if name:
            self._headers["X-Phonectl-Client"] = name
        self._timeout_s = timeout_s
        self._conn: Optional[http.client.HTTPConnection] = None
        self._last_used = 0.0
        self._id = 0

    # --- plumbing -----------------------------------------------------------------------

    def _request(self, method: str, path: str, body: Optional[bytes]) -> tuple[int, bytes]:
        # A kept-alive connection saves a connect per call. The server closes one after 5 s
        # without a request, so an older one is dropped first rather than found dead after
        # sending. A request is sent again only if it could not be sent at all: once it is out,
        # the input may have reached the phone, and sending it twice would tap twice.
        if self._conn is not None and time.monotonic() - self._last_used > _KEEPALIVE_S:
            self.close()
        for attempt in (1, 2):
            if self._conn is None:
                self._conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=self._timeout_s)
            try:
                self._conn.request(method, path, body, self._headers)
                break
            except OSError as e:
                self.close()
                if attempt == 2:
                    raise PhoneError(f"cannot reach the server on port {self.port} ({e!r}); nothing "
                                     "was sent, so the call can be repeated once it is up") from e
        try:
            r = self._conn.getresponse()
            data = r.read()
        except (http.client.HTTPException, OSError) as e:
            self.close()
            raise PhoneError(f"lost the server while waiting for its answer ({e!r}); the call may "
                             "have reached the phone, so look at the screen before repeating it") from e
        self._last_used = time.monotonic()
        return r.status, data

    def call(self, tool: str, **arguments: Any) -> list[dict]:
        """Call a tool; returns its content blocks ({"type": "text", "text": ...} or images)."""
        self._id += 1
        body = json.dumps({"jsonrpc": "2.0", "id": self._id, "method": "tools/call",
                           "params": {"name": tool, "arguments": arguments}}).encode()
        status, raw = self._request("POST", "/mcp", body)
        if status != 200:
            raise PhoneError(f"server answered {status}: {raw.decode(errors='replace').strip()}")
        message = _json_rpc_message(raw.decode())
        if "error" in message:
            raise PhoneError(message["error"].get("message", str(message["error"])))
        result = message["result"]
        content = result.get("content", [])
        text = "".join(c.get("text", "") for c in content if c.get("type") == "text")
        if result.get("isError"):
            cause = _SDK_PREFIX.sub("", text)  # the error's own "Type: message"
            kind = (Busy if cause.startswith("Busy:") else TextTimeout if cause.startswith("TimeoutError:")
                    else ScreenChanged if cause.startswith("NOT DONE:") else PhoneError)
            raise kind(cause)
        return content

    def _text(self, tool: str, **arguments: Any) -> str:
        return "".join(c.get("text", "") for c in self.call(tool, **arguments) if c.get("type") == "text")

    def _admin(self, method: str, path: str, payload: Optional[dict] = None) -> dict:
        body = json.dumps(payload).encode() if payload is not None else None
        status, raw = self._request(method, f"/phonectl/{path}", body)
        if status != 200:
            raise PhoneError(f"server answered {status}: {raw.decode(errors='replace').strip()}")
        return json.loads(raw)

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def release(self) -> bool:
        """Give the phone up now rather than when the server's idle time runs out. A script run
        by an agent counts as that agent, so this leaves the agent's hold alone (False)."""
        return bool(self._admin("POST", "release").get("released"))

    def use(self) -> str:
        """Who is using the phone ("free", "in use by ...")."""
        return self._admin("GET", "use")["use"]

    def __enter__(self) -> "Phone":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        try:
            self.release()
        except (PhoneError, OSError) as e:
            if exc is None:
                raise
            # Leaving on an error already: keep that one, and note why releasing failed too.
            exc.add_note(f"releasing the phone failed as well: {e!r}")
        finally:
            self.close()

    # --- the screen ---------------------------------------------------------------------

    def read_text(self) -> list[dict]:
        """OCR lines {"t", "x", "y", "w", "h"} in points; (x, y) is the centre."""
        return json.loads(self._text("read_text"))

    def has_text(self, text: str) -> bool:
        return any(text in line["t"] for line in self.read_text())

    def wait_text(self, text: str, timeout_s: float = 10, gone: bool = False) -> list[dict]:
        """Lines containing text once it shows (gone=True: once it is gone). PhoneError on timeout."""
        return json.loads(self._text("wait_text", text=text, timeout_s=timeout_s, gone=gone))

    def screenshot(self) -> bytes:
        """The screen as a JPEG, one pixel per point."""
        img = next(c for c in self.call("screenshot") if c.get("type") == "image")
        return base64.b64decode(img["data"])

    # --- input --------------------------------------------------------------------------

    def tap_text(self, text: str, timeout_s: float = 0, settle_ms: Optional[int] = None) -> str:
        return self._text("tap_text", text=text, timeout_s=timeout_s, screenshot=False, settle_ms=settle_ms)

    def tap(self, x: float, y: float, settle_ms: Optional[int] = None) -> str:
        return self._text("tap", x=x, y=y, screenshot=False, settle_ms=settle_ms)

    def long_press(self, x: float, y: float, duration_ms: int = 800, settle_ms: Optional[int] = None) -> str:
        return self._text("long_press", x=x, y=y, duration_ms=duration_ms, screenshot=False, settle_ms=settle_ms)

    def swipe(self, x1: float, y1: float, x2: float, y2: float, duration_ms: int = 300,
              settle_ms: Optional[int] = None) -> str:
        return self._text("swipe", x1=x1, y1=y1, x2=x2, y2=y2, duration_ms=duration_ms,
                          screenshot=False, settle_ms=settle_ms)

    def type_text(self, text: str, method: str = "paste", settle_ms: Optional[int] = None) -> str:
        return self._text("type_text", text=text, method=method, screenshot=False, settle_ms=settle_ms)

    def press_key(self, key: str, settle_ms: Optional[int] = None) -> str:
        return self._text("press_key", key=key, screenshot=False, settle_ms=settle_ms)

    def press_button(self, name: str, settle_ms: Optional[int] = None) -> str:
        return self._text("press_button", name=name, screenshot=False, settle_ms=settle_ms)

    def launch_app(self, bundle_id: str, restart: bool = False, url: Optional[str] = None,
                   settle_ms: Optional[int] = None) -> str:
        return self._text("launch_app", bundle_id=bundle_id, restart=restart, url=url,
                          screenshot=False, settle_ms=settle_ms)

    def get_clipboard(self) -> str:
        return self._text("get_clipboard")

    def set_clipboard(self, text: str) -> None:
        self._text("set_clipboard", text=text)


def _json_rpc_message(raw: str) -> dict:
    """The JSON-RPC reply in a POST response: plain JSON, or the data of an SSE message."""
    raw = raw.strip()
    if raw.startswith("{"):
        return json.loads(raw)
    data = [line[5:].strip() for line in raw.splitlines() if line.startswith("data:")]
    for d in reversed(data):
        msg = json.loads(d)
        if "result" in msg or "error" in msg:
            return msg
    raise PhoneError(f"no reply in the server's answer: {raw[:200]}")
