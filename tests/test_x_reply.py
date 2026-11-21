"""The screen reading in examples/x_reply.py, on lines as OCR gave them on the phone."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "examples"))
import x_reply  # noqa: E402

WORDS = "This felt like years ago"


def _lines(*rows):
    return [{"t": t, "x": 200, "y": y, "w": 300, "h": 17} for t, y in rows]


def test_finds_the_post_below_separate_tabs():
    lines = _lines(('"This felt like years ago"', 86), ("最新", 139), ("用户", 138),
                   ("This felt like years ago... it was this summer", 204))
    assert x_reply.find_post(lines, WORDS)["y"] == 204


def test_finds_the_post_when_the_tabs_are_read_as_one_line():
    lines = _lines(('"This felt like years ago"', 86), ("热门 最新用户视频", 139),
                   ("This felt like years ago... it was this summer", 204))
    assert x_reply.find_post(lines, WORDS)["y"] == 204


def test_the_search_field_is_not_taken_for_the_post():
    assert x_reply.find_post(_lines(('"This felt like years ago"', 86), ("最新", 139)), WORDS) is None


def test_no_results_is_not_a_post():
    class Screen:
        def read_text(self):
            return _lines(('"This felt like years ago"', 86), ("最新", 139),
                          ('"This felt like years ago" 没', 300), ("有搜索到结果", 320))

    assert x_reply.wait_post(Screen(), WORDS, 0) is None


def test_search_words_leave_out_emoji_and_colons():
    assert x_reply.search_words("GOOGLE 🔥: Usage limits") == "GOOGLE Usage limits"
