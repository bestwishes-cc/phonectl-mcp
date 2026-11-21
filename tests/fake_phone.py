"""The HTTP server on a fake phone, for tests that need real clients in other processes.

    PHONECTL_PORT=47390 PHONECTL_TOKEN_FILE=/tmp/t python tests/fake_phone.py [input-log]

Every input the server sends is appended to input-log as one line ("tap 120 300"), so a test
can see what reached the "phone" and in what order.
"""

import io
import sys
from pathlib import Path

from PIL import Image

from phonectl import server

W, H = 402 * 3, 874 * 3  # an iPhone 17 Pro screen, in pixels


class FakeDevice:
    transport = "fake"

    def __init__(self, log: Path | None):
        self._log = log
        buf = io.BytesIO()
        Image.new("RGB", (W, H), "white").save(buf, "PNG")
        self._png = buf.getvalue()
        self._clipboard = ""

    def _note(self, *what) -> None:
        if self._log:
            with self._log.open("a") as f:
                f.write(" ".join(str(w) for w in what) + "\n")

    async def screenshot_png(self) -> bytes:
        return self._png

    async def tap(self, x, y, hold_s=0.05):
        self._note("tap", x, y)

    async def drag(self, x1, y1, x2, y2, duration_s):
        self._note("drag", x1, y1, x2, y2)

    async def key(self, mod_bits, usage):
        self._note("key", mod_bits, usage)

    async def type_ascii(self, text):
        self._note("type", text)

    async def press_button(self, name):
        self._note("button", name)

    async def launch_app(self, bundle_id, restart=False, url=None):
        self._note("launch", bundle_id, url or "")
        return []

    async def list_apps(self):
        return [{"name": "Settings", "bundleIdentifier": "com.apple.Preferences"}]

    async def set_clipboard(self, text):
        self._clipboard = text

    async def get_clipboard(self):
        return self._clipboard


if __name__ == "__main__":
    server.device = FakeDevice(Path(sys.argv[1]) if len(sys.argv) > 1 else None)
    server._serve_http()
