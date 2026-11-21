#!/usr/bin/env python3
"""Reply to a post in the X app on the phone, as one command: an example of a flow script.

    python examples/x_reply.py https://x.com/someone/status/123 "words from the post" "the reply" [--dry-run]

It finds the post with X search (from:someone "words from the post"), opens it, taps the reply
field, pastes the text, checks it and sends it; --dry-run clears the text instead of
sending it. (X ignores post links handed to it at launch: twitter://status and https links both
leave it on the home timeline, while twitter://search works.) An agent that would otherwise take
twenty-odd model round trips for this runs one command and reads one line.

It prints one JSON line and exits 0 when the reply went out ("sent") or the dry run reached
the send button ("dry_run"). Otherwise it exits 1 with "not_found" (no such post in the
search), "restricted" (the author limits who can reply) or "failed": the line says at which
step and what text was on the screen, so the agent that ran it can look and take over. The phone is shared: the script
waits while another client is using it and gives it up when done.
"""

from __future__ import annotations

import argparse
import difflib
import json
import re
import sys
import time
from urllib.parse import quote, urlparse

from phonectl.client import Phone, PhoneError

X = "com.atebits.Tweetie2"
# The app shows Chinese or English depending on the phone's language.
REPLY_FIELD = ("发布你", "Post your reply")  # OCR at times drops the 的 of 发布你的回复
LATEST = ("最新", "Latest")  # a tab of the search results, above the posts
NO_RESULTS = ("没有搜索到结果", "No results for")
RESTRICTED = ("可以回复", "can reply")  # "只有部分账号可以回复", "Only some accounts can reply"
REPLY_BOX = ("回复 @", "Replying to")  # over the text field once the reply box is open
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


def search_words(words: str) -> str:
    """words without what breaks an X phrase search: emoji, colons and other symbols. In Chinese
    text, spaces and Latin words between the characters also break it: keep the longest run of
    Chinese characters then."""
    han = max(re.findall(r"[\u4e00-\u9fff]+", words), key=len, default="")
    if len(han) >= 4:
        return han
    return " ".join(re.sub(r"[^\w\s'.,!?-]", " ", words).split())


def shows(lines: list[dict], text: str, n: int = 16) -> bool:
    """Whether the screen shows the first n letters and digits of text, allowing for OCR misreads
    (按 read as 接): some stretch of the screen matches at least 80% of them."""
    want = _norm(text)[:n]
    screen = _norm("".join(line["t"] for line in lines))
    if len(want) < 6 or want in screen:  # too short to allow a misread
        return bool(want) and want in screen
    m = len(want)
    return any(difflib.SequenceMatcher(None, want, screen[i:i + m]).ratio() >= 0.8
               for i in range(max(1, len(screen) - m + 1)))


def find_post(lines: list[dict], words: str) -> dict | None:
    """The line of a search result (below the result tabs) that begins the post's text.
    OCR reads the tabs as one line ("最新用户视频") or as several ("最新"), so look for the tab
    word in a line, but not in a line that holds the words searched for (the search field)."""
    want = _norm(words)[:16]
    if not want:
        return None
    tabs = [line["y"] for line in lines
            if any(t in line["t"] for t in LATEST) and want not in _norm(line["t"])]
    if not tabs:
        return None
    return next((line for line in lines if line["y"] > min(tabs) and
                 (want in _norm(line["t"]) or _norm(line["t"]) in want and len(_norm(line["t"])) >= 6)), None)


def wait_post(phone: Phone, words: str, timeout_s: float) -> dict | None:
    deadline = time.monotonic() + timeout_s
    while True:
        lines = phone.read_text()
        screen = "".join(line["t"] for line in lines).replace(" ", "")
        if any(t.replace(" ", "") in screen for t in NO_RESULTS):
            return None  # OCR may split that message over lines; it quotes the words searched for
        hit = find_post(lines, words)
        if hit or time.monotonic() >= deadline:
            return hit
        time.sleep(0.2)


def _settled(phone: Phone, words: str, hit: dict) -> dict | None:
    """The post's line once it is at the same place in two reads in a row (or None if gone)."""
    for _ in range(10):
        time.sleep(0.3)
        again = find_post(phone.read_text(), words)
        if again is None or abs(again["y"] - hit["y"]) < 2:
            return again
        hit = again
    return hit


def run(phone: Phone, url: str, words: str, reply: str, dry_run: bool) -> str:
    progress = {"step": "start"}
    try:
        return _steps(phone, url, words, reply, dry_run, progress)
    except PhoneError as e:  # a tool call failed: say during which step
        raise Stop("failed", progress["step"], str(e)) from e


def _steps(phone: Phone, url: str, words: str, reply: str, dry_run: bool, progress: dict) -> str:
    progress["step"] = step = "search for the post"
    path = urlparse(url).path.strip("/").split("/")
    if len(path) < 3 or path[1] != "status":
        raise Stop("failed", step, f"not a post link: {url}")
    words = search_words(words)
    query = f'from:{path[0]} "{words}"'
    phone.launch_app(X, url="twitter://search?query=" + quote(query))
    hit = wait_post(phone, words, 10)
    if hit is None:
        raise Stop("not_found", step, f"no post found with {query}")

    progress["step"] = step = "open the post"
    found = None
    for _ in range(3):  # a tap while the results still move is at times not taken
        hit = _settled(phone, words, hit)
        if hit is None:  # the results are gone: the post is opening
            found = wait_any(phone, REPLY_FIELD + RESTRICTED, 7)
            break
        phone.tap(hit["x"], hit["y"])
        found = wait_any(phone, REPLY_FIELD + RESTRICTED, 3)
        if found is not None:
            break
        hit = find_post(phone.read_text(), words)
        if hit is None:
            found = wait_any(phone, REPLY_FIELD + RESTRICTED, 7)
            break
    if found is None:
        raise Stop("failed", step, "the post did not open (no reply field)")
    lines = found[1]
    if not (shows(lines, words) and _norm(path[0]) in _norm("".join(line["t"] for line in lines))):
        raise Stop("failed", step, f"the page that opened is not @{path[0]}'s post with these words")
    if found[0] in RESTRICTED:
        raise Stop("restricted", step, "the author limits who can reply")

    progress["step"] = step = "open the reply box"
    phone.tap_text(found[0])
    if wait_any(phone, REPLY_BOX, 5) is None:
        raise Stop("failed", step, "no reply box after tapping the reply field")

    progress["step"] = step = "paste the reply"
    phone.type_text(reply, settle_ms=300)
    popup = wait_any(phone, ALLOW_PASTE, 1.5)  # iOS asks the first time an app reads the clipboard
    if popup is not None:
        phone.tap_text(popup[0], settle_ms=300)

    progress["step"] = step = "check the text"
    if not shows(phone.read_text(), reply):
        raise Stop("failed", step, f"the pasted text is not on the screen (looked for {_norm(reply)[:16]!r})")

    if dry_run:
        progress["step"] = step = "leave without sending"
        phone.press_key("cmd+a")
        phone.press_key("backspace", settle_ms=300)
        if shows(phone.read_text(), reply):
            raise Stop("failed", step, "the text is still in the reply box")
        return "dry_run"  # the reply box stays open and empty, so X keeps no draft

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
    ap.add_argument("words", help="a few words from the start of the post, to find it with "
                    "(emoji and symbols are left out of the search)")
    ap.add_argument("reply", help="the reply text, pasted as it is")
    ap.add_argument("--dry-run", action="store_true", help="do everything but send")
    args = ap.parse_args()
    t0 = time.monotonic()
    out: dict = {}
    with Phone() as phone:
        try:
            out = {"status": run(phone, args.url, args.words, args.reply, args.dry_run)}
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
