#!/usr/bin/env python3
"""Reply to a post in the X app on the phone, as one command: an example of a flow script.

    python examples/x_reply.py https://x.com/someone/status/123 "the reply" [--dry-run]

It opens the post by its link, taps the reply field, pastes the text, checks it and sends it;
--dry-run clears the text and leaves instead of sending. An agent that would otherwise take
twenty-odd model round trips for this runs one command and reads one line.

It prints one JSON line and exits 0 when the reply went out (or the dry run reached the send
button), 1 when it stopped: the line says at which step and what text was on the screen, so
the agent that ran it can look and take over from there. The phone is shared: the script
waits while another client is using it and gives it up when done.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time

from phonectl.client import Phone, PhoneError

X = "com.atebits.Tweetie2"
# The app shows Chinese or English depending on the phone's language.
REPLY_FIELD = ("发布你的回复", "Post your reply")
RESTRICTED = ("可以回复", "can reply")  # "只有部分账号可以回复", "Only some accounts can reply"
CANCEL = ("取消", "Cancel")
SEND = ("回复", "Reply")
ALLOW_PASTE = ("允许粘贴", "Allow Paste")
SENT = ("已发送", "was sent")


class Stop(Exception):
    def __init__(self, status: str, step: str, detail: str = ""):
        super().__init__(detail)
        self.status, self.step, self.detail = status, step, detail


def _norm(text: str) -> str:
    """Letters and digits only: OCR drops or alters spaces and punctuation."""
    return re.sub(r"[\W_]+", "", text).lower()


def wait_any(phone: Phone, texts: tuple[str, ...], timeout_s: float) -> tuple[str, list[dict]] | None:
    """The first of texts to show on the screen and the lines then, or None after timeout_s."""
    deadline = time.monotonic() + timeout_s
    while True:
        lines = phone.read_text()
        for t in texts:
            if any(t in line["t"] for line in lines):
                return t, lines
        if time.monotonic() >= deadline:
            return None
        time.sleep(0.2)


def run(phone: Phone, url: str, reply: str, dry_run: bool) -> str:
    progress = {"step": "start"}
    try:
        return _steps(phone, url, reply, dry_run, progress)
    except PhoneError as e:  # a tool call failed: say during which step
        raise Stop("failed", progress["step"], str(e)) from e


def _steps(phone: Phone, url: str, reply: str, dry_run: bool, progress: dict) -> str:
    progress["step"] = step = "open the post"
    phone.launch_app(X, url=url)
    found = wait_any(phone, REPLY_FIELD + RESTRICTED, 15)
    if found is None:
        raise Stop("failed", step, "the post did not open (no reply field)")
    if found[0] in RESTRICTED:
        raise Stop("restricted", step, "the author limits who can reply")

    progress["step"] = step = "open the reply box"
    phone.tap_text(found[0])
    if wait_any(phone, CANCEL, 5) is None:
        raise Stop("failed", step, "no reply box after tapping the reply field")

    progress["step"] = step = "paste the reply"
    phone.type_text(reply, settle_ms=300)
    popup = wait_any(phone, ALLOW_PASTE, 1.5)  # iOS asks the first time an app reads the clipboard
    if popup is not None:
        phone.tap_text(popup[0], settle_ms=300)

    progress["step"] = step = "check the text"
    want = _norm(reply)[:12]
    screen = _norm("".join(line["t"] for line in phone.read_text()))
    if want not in screen:
        raise Stop("failed", step, f"the pasted text is not on the screen (looked for {want!r})")

    if dry_run:
        progress["step"] = step = "leave without sending"
        phone.press_key("cmd+a")
        phone.press_key("backspace", settle_ms=200)
        cancel = wait_any(phone, CANCEL, 2)
        if cancel is None:
            raise Stop("failed", step, "no Cancel button to leave the reply box")
        phone.tap_text(cancel[0], settle_ms=500)
        return "dry_run"

    progress["step"] = step = "send"
    lines = phone.read_text()
    button = next((t for t in SEND if any(line["t"].strip() == t for line in lines)), None)
    if button is None:
        raise Stop("failed", step, "no Reply button")
    phone.tap_text(button)
    if wait_any(phone, SENT + REPLY_FIELD, 10) is None:
        raise Stop("failed", step, "no sign that the reply was sent")
    return "sent"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("url", help="the post's link, https://x.com/<user>/status/<id>")
    ap.add_argument("reply", help="the reply text, pasted as it is")
    ap.add_argument("--dry-run", action="store_true", help="do everything but send")
    args = ap.parse_args()
    t0 = time.monotonic()
    out: dict = {}
    with Phone() as phone:
        try:
            out = {"status": run(phone, args.url, args.reply, args.dry_run)}
        except Stop as e:
            out = {"status": e.status, "step": e.step, "detail": e.detail}
        if out["status"] not in ("sent", "dry_run"):
            try:  # what the agent taking over will see first
                out["screen"] = [line["t"] for line in phone.read_text()][:40]
            except PhoneError as e:
                out["screen_error"] = str(e)
    out["seconds"] = round(time.monotonic() - t0, 1)
    print(json.dumps(out, ensure_ascii=False))
    return 0 if out["status"] in ("sent", "dry_run") else 1


if __name__ == "__main__":
    sys.exit(main())
