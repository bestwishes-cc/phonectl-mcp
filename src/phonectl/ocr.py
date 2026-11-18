"""Text recognition on a screenshot with Apple Vision, in this process (no helper binary).

Lines come back with their box centre and size in the image's pixels, origin top-left.
"""

from __future__ import annotations

import os

import objc
import Vision
from Foundation import NSData, NSMakeRange

# Recognition languages in priority order, e.g. "en-US" or "ja-JP,en-US".
LANGUAGES = [s.strip() for s in os.environ.get("PHONECTL_OCR_LANGS", "zh-Hans,en-US").split(",") if s.strip()]


def _utf16_len(s: str) -> int:
    """Length in UTF-16 units, which NSRange counts; Python counts code points (differ for emoji)."""
    return len(s.encode("utf-16-le")) // 2


def _box(rect, width: int, height: int) -> dict:
    """Vision's normalised rect (origin bottom-left) -> centre and size in pixels, origin top-left."""
    (bx, by), (bw, bh) = rect
    return {
        "x": round((bx + bw / 2) * width),
        "y": round((1 - (by + bh / 2)) * height),
        "w": round(bw * width),
        "h": round(bh * height),
    }


def recognize(png: bytes, width: int, height: int, find: str = "") -> list[dict]:
    """One dict per recognised line: {"t": text, "x": cx, "y": cy, "w": w, "h": h} in pixels.

    With find, only lines containing it, and the box is that of find's first occurrence in the
    line: Vision often joins neighbouring buttons into one line ("Cancel  OK"), whose centre
    would fall between them.
    """
    with objc.autorelease_pool():
        data = NSData.dataWithBytes_length_(png, len(png))
        handler = Vision.VNImageRequestHandler.alloc().initWithData_options_(data, None)
        req = Vision.VNRecognizeTextRequest.alloc().init()
        req.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelAccurate)
        req.setRecognitionLanguages_(LANGUAGES)
        req.setUsesLanguageCorrection_(False)
        ok, err = handler.performRequests_error_([req], None)
        if not ok:
            raise RuntimeError(f"text recognition failed: {err}")
        lines = []
        for obs in req.results() or []:
            cands = obs.topCandidates_(1)
            if not cands:
                continue
            text = cands[0].string()
            rect = obs.boundingBox()
            if find:
                start = text.find(find)
                if start < 0:
                    continue
                sub, _ = cands[0].boundingBoxForRange_error_(
                    NSMakeRange(_utf16_len(text[:start]), _utf16_len(find)), None
                )
                if sub is not None:
                    rect = sub.boundingBox()
            lines.append({"t": text, **_box(rect, width, height)})
        return lines
